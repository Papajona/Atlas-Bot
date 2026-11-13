"""Property-based tests for money-safety invariants.

Example-based tests pin known cases; these generate many cases and check laws that must always hold:
 * a withdrawal proposal digest changes if and only if a money-relevant field changes,
 * the separation-of-duties rule cannot be dodged by case or whitespace,
 * solvency is monotone in assets,
 * any mix of withdrawal reserve/release/settle calls, including retries, keeps balances non-negative, conserves
   funds, and leaves the ledger invariant report clean.
Needs `hypothesis` (listed in requirements-dev.txt). The repo forbids skipped tests, so a missing dependency fails loudly.
"""
import asyncio
from decimal import Decimal
from unittest import mock

from hypothesis import HealthCheck, given, settings as hyp_settings, strategies as st

from app.config import settings
from app.custody_reconciliation import solvency_decision
from app.withdrawal_security import (
    destination_fingerprint, proposal_digest, release_separation_violation,
)

ident = st.text(alphabet=st.characters(min_codepoint=48, max_codepoint=122, blacklist_categories=("Cs",)),
                min_size=1, max_size=12)
amount = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1000000"), places=6, allow_nan=False, allow_infinity=False)
BASE = dict(request_id="r1", amount=Decimal("10"), currency="USDT", destination="Tabc", tag="", network="TRON", provider="p")


# ---------------------------------------------------------------- proposal digest
@hyp_settings(max_examples=100, deadline=None)
@given(request_id=ident, amt=amount, destination=ident, tag=st.text(max_size=8), provider=ident)
def test_digest_is_deterministic_and_ignores_decimal_formatting(request_id, amt, destination, tag, provider):
    kw = dict(request_id=request_id, amount=amt, currency="USDT", destination=destination, tag=tag, network="TRON", provider=provider)
    assert proposal_digest(**kw) == proposal_digest(**kw)
    assert proposal_digest(**kw) == proposal_digest(**{**kw, "amount": str(amt) + "000"})
    assert proposal_digest(**kw) == proposal_digest(**{**kw, "currency": "usdt"})


@hyp_settings(max_examples=100, deadline=None)
@given(field=st.sampled_from(["request_id", "destination", "tag", "network", "provider"]), other=ident)
def test_changing_any_text_field_changes_the_digest(field, other):
    changed = {**BASE, field: str(BASE[field]) + "|" + other}
    assert proposal_digest(**changed) != proposal_digest(**BASE)


@hyp_settings(max_examples=100, deadline=None)
@given(a=amount, b=amount)
def test_different_amounts_give_different_digests(a, b):
    same = a.normalize() == b.normalize()
    assert (proposal_digest(**{**BASE, "amount": a}) == proposal_digest(**{**BASE, "amount": b})) == same


@hyp_settings(max_examples=100, deadline=None)
@given(dest=ident, pad=st.text(alphabet=" \t", max_size=3))
def test_fingerprint_ignores_surrounding_whitespace_and_asset_case(dest, pad):
    assert destination_fingerprint(pad + dest + pad, "usdt", "tron") == destination_fingerprint(dest, "USDT", "TRON")
    assert destination_fingerprint(dest, "USDT", "TRON") != destination_fingerprint(dest + "x", "USDT", "TRON")


# ---------------------------------------------------------------- separation of duties
@hyp_settings(max_examples=100, deadline=None)
@given(operator=ident, others=st.lists(ident, max_size=3), pad=st.text(alphabet=" ", max_size=2), upper=st.booleans())
def test_release_operator_can_never_be_an_approver(operator, others, pad, upper):
    disguised = pad + (operator.upper() if upper else operator.lower()) + pad
    assert release_separation_violation(operator, None, others + [disguised]) is not None


@hyp_settings(max_examples=100, deadline=None)
@given(operator=ident, approvers=st.lists(ident, max_size=3))
def test_distinct_operator_with_no_shared_credential_is_allowed(operator, approvers):
    approvers = [a for a in approvers if a.strip().lower() != operator.strip().lower()]
    with mock.patch.object(settings, "withdrawal_approver_tokens", ""):
        assert release_separation_violation(operator, "some-token", approvers) is None


# ---------------------------------------------------------------- solvency
money = st.decimals(min_value=Decimal("0"), max_value=Decimal("10000000"), places=6, allow_nan=False, allow_infinity=False)


@hyp_settings(max_examples=150, deadline=None)
@given(assets=money, liabilities=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("10000000"), places=6),
       extra=money, ratio=st.decimals(min_value=Decimal("0.5"), max_value=Decimal("2"), places=3))
def test_solvency_is_monotone_in_assets_and_gap_is_exact(assets, liabilities, extra, ratio):
    low = solvency_decision(assets=assets, liabilities=liabilities, minimum_ratio=ratio)
    high = solvency_decision(assets=assets + extra, liabilities=liabilities, minimum_ratio=ratio)
    assert low["coverage_gap"] == assets - liabilities
    assert (low["status"] == "OK") == (assets / liabilities >= ratio)
    if low["status"] == "OK":
        assert high["status"] == "OK"


# ---------------------------------------------------------------- ledger lifecycle
async def _run_lifecycle(operations):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.customer_funds import (
        USDT, _customer_account, get_or_create_ledger, ledger_invariant_report, post_journal,
        release_withdrawal, reserve_withdrawal, settle_withdrawal,
    )
    from app.db import Base, CustomerLedgerAccount

    funded = Decimal("1000")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions() as seed_db:
            ledger = await get_or_create_ledger(seed_db, 1, USDT)
            await post_journal(seed_db, currency=USDT, entry_type="DEPOSIT", reference_type="DEPOSIT", reference_id="d1",
                               idempotency_key="dep:1", description="seed",
                               lines=[{"account_code": "ASSET:CUSTODY:USDT", "debit": 1000, "credit": 0},
                                      {"account_code": _customer_account(1, "AVAILABLE"), "customer_id": 1, "debit": 0, "credit": 1000}])
            ledger.available = funded
            await seed_db.commit()

        async def snapshot():
            async with sessions() as read_db:
                row = (await read_db.execute(select(CustomerLedgerAccount))).scalar_one()
                return Decimal(str(row.available)), Decimal(str(row.withdrawal_reserved))

        settled = Decimal("0")
        for index, (amt, outcome, replays) in enumerate(operations):
            ref, amt_d = f"w{index}", Decimal(amt)
            before = await snapshot()
            try:
                for _ in range(replays):
                    async with sessions() as reserve_db:
                        await reserve_withdrawal(reserve_db, 1, amt_d, reference_id=ref)
                        await reserve_db.commit()
            except ValueError:
                assert await snapshot() == before, "a rejected reserve must change nothing"
                continue

            if outcome == "release":
                for _ in range(replays):
                    async with sessions() as release_db:
                        await release_withdrawal(release_db, 1, amt_d, reference_id=ref)
                        await release_db.commit()
            elif outcome == "settle":
                for _ in range(replays):
                    async with sessions() as settle_db:
                        await settle_withdrawal(settle_db, 1, amt_d, reference_id=ref)
                        await settle_db.commit()
                settled += amt_d

            available, reserved = await snapshot()
            assert available >= 0 and reserved >= 0
            assert available + reserved + settled == funded, "funds must be conserved"
            async with sessions() as audit_db:
                assert (await ledger_invariant_report(audit_db))["ok"] is True
    finally:
        await engine.dispose()



operation = st.tuples(
    st.decimals(min_value=Decimal("0.000001"), max_value=Decimal("600"), places=6, allow_nan=False, allow_infinity=False),
    st.sampled_from(["pending", "release", "settle"]),
    st.integers(1, 3),
)


@hyp_settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(operations=st.lists(operation, min_size=1, max_size=8))
def test_withdrawal_lifecycle_conserves_funds_under_retries(operations):
    from cryptography.fernet import Fernet
    with mock.patch.object(settings, "app_encryption_key", Fernet.generate_key().decode()), \
            mock.patch.object(settings, "app_encryption_keys_json", ""), \
            mock.patch.object(settings, "app_encryption_active_key_id", "v1"):
        asyncio.run(_run_lifecycle(operations))

@hyp_settings(max_examples=100, deadline=None)
@given(secret=st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    min_size=1,
    max_size=32,
))
def test_release_credential_can_never_reuse_any_approver_credential(secret):
    with mock.patch.object(settings, "withdrawal_approver_tokens", f"approver:{secret},backup:{secret[::-1]}"):
        assert release_separation_violation(
            "independent-operator", secret, ["approver", "backup"]
        ) is not None

