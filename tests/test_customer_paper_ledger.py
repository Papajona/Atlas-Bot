from __future__ import annotations

import asyncio
import os
from decimal import Decimal

import pytest

from app.config import settings
from app.customer_paper_ledger import (
    CustomerPaperIdempotencyConflict,
    _tenant_policy_expression_is_exact,
    _validated_paper_journal,
    get_customer_paper_balances,
    post_customer_paper_journal,
    preflight_isolated_customer_paper_ledger,
    validate_database_url_pair,
)


def _seed_lines(amount: str = "100"):
    return [
        {"account_code": "PAPER_CASH", "signed_amount": amount},
        {"account_code": "PAPER_EQUITY", "signed_amount": str(-Decimal(amount))},
    ]


def test_customer_paper_database_pair_rejects_same_database_even_with_separate_role():
    primary = "postgresql+asyncpg://atlas_primary:primary_password@db.example:5432/atlas"
    paper = "postgresql+asyncpg://atlas_paper:paper_password@db.example:5432/atlas"
    with pytest.raises(ValueError, match="separate PostgreSQL database"):
        validate_database_url_pair(primary, paper)


def test_customer_paper_database_pair_allows_distinct_databases_on_same_server():
    primary = "postgresql+asyncpg://atlas_primary:primary_password@db.example:5432/atlas"
    paper = "postgresql+asyncpg://atlas_paper:paper_password@db.example:5432/atlas_paper"
    pair = validate_database_url_pair(primary, paper)
    assert pair["primary_database"] == "atlas"
    assert pair["paper_database"] == "atlas_paper"
    assert pair["same_server"] is True
    assert pair["distinct_runtime_roles"] is True
    assert pair["distinct_credentials"] is True


def test_customer_paper_journal_validation_is_exact_and_balanced():
    payload = _validated_paper_journal(
        customer_id=7,
        idempotency_key="seed-7",
        event_type="PAPER_SEED",
        currency="USDT",
        lines=_seed_lines("1.000000000000000001"),
        reference="paper-seed",
        metadata={"note": "test"},
    )
    assert payload["payload_sha256"]
    assert payload["lines"][0]["signed_amount"] == "1.000000000000000001"
    with pytest.raises(ValueError, match="sum exactly to zero"):
        _validated_paper_journal(
            customer_id=7, idempotency_key="unbalanced", event_type="PAPER_SEED",
            currency="USDT",
            lines=[
                {"account_code": "PAPER_CASH", "signed_amount": "2"},
                {"account_code": "PAPER_EQUITY", "signed_amount": "-1"},
            ],
            reference="", metadata=None,
        )
    with pytest.raises(ValueError, match="exact decimal"):
        _validated_paper_journal(
            customer_id=7, idempotency_key="float", event_type="PAPER_SEED",
            currency="USDT",
            lines=[
                {"account_code": "PAPER_CASH", "signed_amount": 1.1},
                {"account_code": "PAPER_EQUITY", "signed_amount": "-1.1"},
            ],
            reference="", metadata=None,
        )


def test_customer_paper_ledger_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "customer_paper_ledger_enabled", False)
    result = asyncio.run(preflight_isolated_customer_paper_ledger(require_enabled=True))
    assert result == {
        "status": "BLOCKED",
        "reason": "CUSTOMER_PAPER_LEDGER_FEATURE_DISABLED",
    }


@pytest.mark.skipif(
    not os.getenv("ATLAS_PAPER_TEST_DATABASE_URL"),
    reason="requires the dedicated disposable PostgreSQL paper database provisioned by CI",
)
def test_isolated_customer_paper_ledger_posting_replay_and_tenant_isolation(monkeypatch):
    paper_url = os.environ["ATLAS_PAPER_TEST_DATABASE_URL"]
    monkeypatch.setattr(settings, "database_url", os.environ["ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL"])
    monkeypatch.setattr(settings, "customer_paper_database_url", paper_url)
    monkeypatch.setattr(settings, "customer_paper_ledger_enabled", True)

    async def run():
        preflight = await preflight_isolated_customer_paper_ledger()
        assert preflight["status"] == "PASS", preflight

        first = await post_customer_paper_journal(
            customer_id=91001,
            idempotency_key="stress-seed-91001",
            event_type="PAPER_SEED",
            currency="USDT",
            lines=_seed_lines("100"),
            reference="test-only-paper-seed",
            metadata={"environment": "disposable-ci"},
        )
        assert first["status"] == "APPENDED"

        replay = await post_customer_paper_journal(
            customer_id=91001,
            idempotency_key="stress-seed-91001",
            event_type="PAPER_SEED",
            currency="USDT",
            lines=_seed_lines("100"),
            reference="test-only-paper-seed",
            metadata={"environment": "disposable-ci"},
        )
        assert replay["status"] == "REPLAYED"
        assert replay["journal_id"] == first["journal_id"]

        with pytest.raises(CustomerPaperIdempotencyConflict):
            await post_customer_paper_journal(
                customer_id=91001,
                idempotency_key="stress-seed-91001",
                event_type="PAPER_SEED",
                currency="USDT",
                lines=_seed_lines("101"),
                reference="test-only-paper-seed",
                metadata={"environment": "disposable-ci"},
            )

        await post_customer_paper_journal(
            customer_id=91001,
            idempotency_key="stress-fill-91001",
            event_type="SIMULATED_FILL",
            currency="USDT",
            lines=[
                {"account_code": "PAPER_CASH", "signed_amount": "-2.5"},
                {"account_code": "PAPER_CLEARING", "signed_amount": "2.5"},
            ],
            reference="test-only-paper-fill",
            metadata={"order_id": "synthetic-test-order"},
        )
        balances = await get_customer_paper_balances(customer_id=91001)
        indexed = {(row["currency"], row["account_code"]): Decimal(row["balance"]) for row in balances}
        assert indexed[("USDT", "PAPER_CASH")] == Decimal("97.5")
        assert indexed[("USDT", "PAPER_EQUITY")] == Decimal("-100")
        assert indexed[("USDT", "PAPER_CLEARING")] == Decimal("2.5")

        # RLS hides every account and journal line belonging to a different tenant.
        assert await get_customer_paper_balances(customer_id=91002) == []

    asyncio.run(run())


def test_customer_paper_rls_preflight_accepts_only_exact_tenant_predicate():
    exact = (
        "(customer_id = NULLIF(current_setting('app.customer_id'::text, true), "
        "''::text)::bigint)"
    )
    assert _tenant_policy_expression_is_exact(exact) is True
    assert _tenant_policy_expression_is_exact(
        exact[:-1] + " OR true)"
    ) is False
    assert _tenant_policy_expression_is_exact(
        "(customer_id = NULLIF(current_setting('app.other_id'::text, true), ''::text)::bigint)"
    ) is False
    assert _tenant_policy_expression_is_exact("") is False
