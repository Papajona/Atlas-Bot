"""Behavioral regression tests for the withdrawal-path hardening patch."""
import ast
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.main as main
from app.config import settings
from app.customer_funds import release_withdrawal as ledger_release_withdrawal
from app.db import Base, CustomerLedgerAccount, Withdrawal
from app.payout import CCXTPayoutProvider, PayoutUnknown
from app.withdrawal_ids import next_customer_withdrawal_request_id
from test_withdrawal_not_sent_db import _call_endpoint

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "{}")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")


def _engine():
    return create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)


def _wd(**kw):
    base = dict(customer_id=1, request_id="req", account_ref="acct", amount=25, currency="USDT",
                destination_masked="T***", destination="T" + "A" * 33, network="TRON", provider="ccxt")
    base.update(kw)
    return Withdrawal(**base)


# ---- 1. repeated identical customer withdrawals ---------------------------------------------------

def test_request_id_sequence_is_unique_and_stable():
    base = "cust-wd-7-abc"
    assert next_customer_withdrawal_request_id(base, []) == base
    assert next_customer_withdrawal_request_id(base, [base]) == f"{base}-r1"
    assert next_customer_withdrawal_request_id(base, [base, f"{base}-r1"]) == f"{base}-r2"
    # a gap must never reuse an existing id
    assert next_customer_withdrawal_request_id(base, [base, f"{base}-r2"]) == f"{base}-r3"


def test_three_identical_withdrawals_in_sequence_all_insert_with_unique_constraint():
    async def run():
        engine = _engine()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            base = "cust-wd-7-0123456789abcdef01234567"
            async with sessions() as db:
                for _ in range(3):
                    prior = (await db.execute(select(Withdrawal.request_id).where(Withdrawal.request_id.like(base + "%")))).scalars().all()
                    db.add(_wd(request_id=next_customer_withdrawal_request_id(base, prior), status="RELEASED"))
                    await db.commit()
                ids = (await db.execute(select(Withdrawal.request_id).order_by(Withdrawal.id))).scalars().all()
            assert ids == [base, f"{base}-r1", f"{base}-r2"]
            # the pre-patch behavior: reusing the same id is rejected by the unique constraint
            async with sessions() as db:
                db.add(_wd(request_id=base, status="PENDING"))
                with pytest.raises(IntegrityError):
                    await db.commit()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_customer_endpoint_wiring():
    src = (ROOT / "app" / "main.py").read_text()
    block = src[src.index('@app.post("/api/customer/withdrawals")'):src.index('@app.get("/api/customer/withdrawals")')]
    assert "next_customer_withdrawal_request_id(base_request_id" in block
    assert "except IntegrityError:" in block and "await db.rollback()" in block
    assert block.index("existing = ") < block.index("stepup.used_at =") < block.index("db.add(w)")


# ---- 2. canonical release reference: a withdrawal's reserve can be freed exactly once -------------

def test_release_reference_is_single_and_idempotent_even_with_another_pending_reserve():
    async def run():
        engine = _engine()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                # two withdrawals of 25 each are reserved for the same customer
                db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("0"), withdrawal_reserved=Decimal("50")))
                await db.commit()
                ref = main._withdrawal_release_ref("req-A")
                await ledger_release_withdrawal(db, 1, 25, reference_id=ref)
                await ledger_release_withdrawal(db, 1, 25, reference_id=main._withdrawal_release_ref("req-A"))  # racing path, same ref
                await db.commit()
                acct = (await db.execute(select(CustomerLedgerAccount).where(CustomerLedgerAccount.customer_id == 1))).scalar_one()
                assert acct.withdrawal_reserved == Decimal("25.000000"), "second pending withdrawal's reserve must be untouched"
                assert acct.available == Decimal("25.000000")
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_no_legacy_per_path_release_references_remain():
    src = (ROOT / "app" / "main.py").read_text()
    for legacy in (':failed"', ':reconcile-failed"', ':not-sent"', ':rejected"'):
        assert f'request_id + "{legacy}' not in src
    assert main._withdrawal_release_ref("abc") == "abc:release"


# ---- 3. stuck SUBMITTING / unclassified provider failures -----------------------------------------+
def test_mark_withdrawal_unknown_helper_only_moves_in_flight_rows(monkeypatch):
    async def run():
        engine = _engine()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(main, "SessionLocal", sessions)
            async with sessions() as db:
                a = _wd(request_id="a", status="SUBMITTING"); b = _wd(request_id="b", status="RELEASED"); c = _wd(request_id="c", status="FAILED")
                db.add_all([a, b, c]); await db.commit()
                ids = (a.id, b.id, c.id)
            await main._mark_withdrawal_unknown(ids[0], "ccxt", "boom", provider_id="prov-1")
            await main._mark_withdrawal_unknown(ids[1], "ccxt", "boom")
            await main._mark_withdrawal_unknown(ids[2], "ccxt", "boom")
            async with sessions() as db:
                rows = {r.request_id: r for r in (await db.execute(select(Withdrawal))).scalars()}
            assert rows["a"].status == "UNKNOWN" and rows["a"].provider_id == "prov-1" and "boom" in rows["a"].provider_error
            assert rows["b"].status == "RELEASED" and rows["c"].status == "FAILED", "terminal rows must never be rewritten"
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_release_flow_handles_cancel_unclassified_and_post_send_failure():
    tree = ast.parse((ROOT / "app" / "main.py").read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "release_withdrawal")
    handlers = [ast.unparse(h.type) for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler) and h.type is not None]
    for needed in ("PayoutUnknown", "PayoutError", "asyncio.CancelledError", "Exception", "HTTPException"):
        assert needed in handlers, needed
    text = ast.unparse(fn)
    assert "provider_id=result.provider_id" in text            # post-send failure keeps the provider id
    assert "w2.status != 'SUBMITTING'" in text                  # no release after reconcile resolved it
    assert "WITHDRAWAL_RELEASE_RACE_SKIPPED" in text


def _stale_case(monkeypatch, *, started_delta, expect_status=None, expect_http=None):
    async def run():
        engine = _engine()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("0"), withdrawal_reserved=Decimal("25")))
                w = _wd(request_id="stale", status="SUBMITTING", provider_id=None,
                        execution_started_at=datetime.now(timezone.utc) - started_delta)
                db.add(w); await db.commit(); await db.refresh(w)

                class Provider:
                    async def recover(self, *a, **k):
                        raise PayoutNotFound("provider confirms no matching payout")

                if expect_http:
                    with pytest.raises(HTTPException) as exc:
                        await _call_endpoint(main, sessions, w, provider=Provider(), monkeypatch=monkeypatch)
                    assert exc.value.status_code == expect_http
                else:
                    res = await _call_endpoint(main, sessions, w, provider=Provider(), monkeypatch=monkeypatch)
                    assert res["status"] == expect_status
            async with sessions() as db:
                acct = (await db.execute(select(CustomerLedgerAccount).where(CustomerLedgerAccount.customer_id == 1))).scalar_one()
                return acct.available, acct.withdrawal_reserved
        finally:
            await engine.dispose()
    return asyncio.run(run())


def test_stale_submitting_without_provider_id_can_be_marked_not_sent(monkeypatch):
    available, reserved = _stale_case(monkeypatch, started_delta=timedelta(hours=1), expect_status="FAILED")
    assert (available, reserved) == (Decimal("25.000000"), Decimal("0.000000"))


def test_fresh_submitting_cannot_be_marked_not_sent(monkeypatch):
    available, reserved = _stale_case(monkeypatch, started_delta=timedelta(seconds=30), expect_http=409)
    assert (available, reserved) == (Decimal("0.000000"), Decimal("25.000000"))


# ---- 4. CCXT: absence of history is not proof of non-payment ---------------------------------------

class _FakeExchange:
    id = "fake"
    has = {"withdraw": True, "fetchWithdrawals": True}

    def __init__(self, rows=None):
        self.rows = rows or []
        self.withdraw_params = None
        self.fetch_args = None

    def withdraw(self, currency, amount, destination, tag, params):
        self.withdraw_params = dict(params)
        return {"id": "tx-1", "status": "pending"}

    def fetch_withdrawals(self, currency, since, limit):
        self.fetch_args = (currency, since, limit)
        return self.rows


def _provider(ex):
    p = object.__new__(CCXTPayoutProvider)
    p.exchange = ex
    return p


def test_ccxt_does_not_claim_idempotency_it_cannot_prove(monkeypatch):
    monkeypatch.setattr(settings, "payout_client_id_param", "")
    ex = _FakeExchange()
    p = _provider(ex)
    asyncio.run(p.send(currency="USDT", amount=1, destination="T", tag=None, network="TRON", idempotency_key="k1", metadata={}))
    assert "clientOrderId" not in ex.withdraw_params and "k1" not in ex.withdraw_params.values()
    with pytest.raises(PayoutUnknown) as exc:
        asyncio.run(p.recover("k1", currency="USDT"))
    assert not isinstance(exc.value, PayoutNotFound), "no verified client id => absence must stay UNKNOWN"


def test_ccxt_absence_stays_unknown_with_verified_client_id_and_windowed_lookup(monkeypatch):
    monkeypatch.setattr(settings, "payout_client_id_param", "withdrawOrderId")
    monkeypatch.setattr(settings, "payout_recovery_lookback_hours", 48)
    ex = _FakeExchange()
    p = _provider(ex)
    asyncio.run(p.send(currency="USDT", amount=1, destination="T", tag=None, network="TRON", idempotency_key="k2", metadata={}))
    assert ex.withdraw_params["withdrawOrderId"] == "k2"
    # A configured client-id parameter enables positive correlation, but bounded CCXT history
    # cannot prove absence; releasing a reserve on a missing row would be unsafe.
    with pytest.raises(PayoutUnknown):
        asyncio.run(p.recover("k2", currency="USDT"))
    since = ex.fetch_args[1]
    assert since is not None and abs((datetime.now(timezone.utc).timestamp() * 1000 - 48 * 3600 * 1000) - since) < 60_000
    ex.rows = [{"id": "tx-9", "status": "ok", "info": {"withdrawOrderId": "k2"}}]
    res = asyncio.run(p.recover("k2", currency="USDT"))
    assert res.provider_id == "tx-9" and res.status == "COMPLETED"


# ---- 5. live-enable request expiry -----------------------------------------------------------------

def test_live_enable_request_expires(monkeypatch):
    monkeypatch.setattr(settings, "live_enable_request_ttl_minutes", 15)
    now = datetime.now(timezone.utc)
    assert main._live_request_expired(None, now) is True
    assert main._live_request_expired(now - timedelta(minutes=5), now) is False
    assert main._live_request_expired(now - timedelta(minutes=16), now) is True
    assert main._live_request_expired((now - timedelta(minutes=5)).replace(tzinfo=None), now) is False  # naive DB value


def test_provider_recovery_full_history_page_is_not_proof_of_absence(monkeypatch):
    """A full 100-row CCXT history page may be truncated; never release funds on that basis."""
    monkeypatch.setattr(settings, "payout_client_id_param", "clientOrderId")
    monkeypatch.setattr(settings, "payout_recovery_lookback_hours", 72)
    provider = CCXTPayoutProvider.__new__(CCXTPayoutProvider)
    provider.exchange = SimpleNamespace(
        has={"fetchWithdrawals": True},
        fetch_withdrawals=lambda currency, since, limit: [
            {"id": f"provider-{i}", "info": {"clientOrderId": f"other-{i}"}}
            for i in range(100)
        ],
    )
    with pytest.raises(PayoutUnknown, match="100-row limit"):
        asyncio.run(provider.recover("withdrawal:missing", currency="USDT"))


def test_provider_recovery_short_history_page_does_not_prove_payout_absence(monkeypatch):
    """Even with a configured client-id field, generic bounded history is not authoritative absence."""
    monkeypatch.setattr(settings, "payout_client_id_param", "clientOrderId")
    monkeypatch.setattr(settings, "payout_recovery_lookback_hours", 72)
    provider = CCXTPayoutProvider.__new__(CCXTPayoutProvider)
    provider.exchange = SimpleNamespace(
        has={"fetchWithdrawals": True},
        fetch_withdrawals=lambda currency, since, limit: [
            {"id": "provider-1", "info": {"clientOrderId": "other-1", "memo": "audit note mentions withdrawal:missing but is not its client ID"}},
            {"id": "provider-2", "info": {"clientOrderId": "other-2"}},
        ],
    )
    with pytest.raises(PayoutUnknown, match="absence is not proof"):
        asyncio.run(provider.recover("withdrawal:missing", currency="USDT"))
