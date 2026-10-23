from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import settings
from app import customer_paper_lifecycle as paper_lifecycle
from app.customer_paper_lifecycle import (
    CustomerPaperOrderConflict,
    cancel_customer_paper_order,
    get_customer_paper_order,
    expire_customer_paper_order,
    get_customer_paper_positions,
    list_open_customer_paper_orders,
    record_customer_paper_fill,
    reverse_customer_paper_fill,
    submit_customer_paper_order,
)
from app.customer_paper_ledger import (
    CustomerPaperIdempotencyConflict,
    _tenant_policy_expression_is_exact,
    _tenant_policy_validation_error,
    _validated_paper_journal,
    _configured_database_role,
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


def test_public_customer_paper_journal_writer_only_accepts_positive_seed():
    valid = _seed_lines("10")

    with pytest.raises(ValueError, match="only PAPER_SEED"):
        asyncio.run(post_customer_paper_journal(
            customer_id=7,
            idempotency_key="forbidden-direct-fill-journal",
            event_type="SIMULATED_FILL",
            currency="USDT",
            lines=valid,
        ))

    with pytest.raises(ValueError, match="positive PAPER_CASH"):
        asyncio.run(post_customer_paper_journal(
            customer_id=7,
            idempotency_key="negative-cash-seed",
            event_type="PAPER_SEED",
            currency="USDT",
            lines=[
                {"account_code": "PAPER_CASH", "signed_amount": "-10"},
                {"account_code": "PAPER_EQUITY", "signed_amount": "10"},
            ],
        ))


def test_customer_paper_database_pair_rejects_same_database_even_with_separate_role():
    primary = "postgresql+asyncpg://atlas_primary:primary_password@db.example:5432/atlas"
    paper = "postgresql+asyncpg://atlas_paper:paper_password@db.example:5432/atlas"
    with pytest.raises(ValueError, match="separate PostgreSQL database"):
        validate_database_url_pair(primary, paper)


def test_customer_paper_shared_database_mode_requires_explicit_opt_in_and_distinct_credentials():
    primary = "postgresql+asyncpg://atlas_primary:primary_password@db.example:5432/postgres"
    paper = "postgresql+asyncpg://atlas_paper:paper_password@db.example:5432/postgres"

    with pytest.raises(ValueError, match="explicitly enabled"):
        validate_database_url_pair(primary, paper)

    pair = validate_database_url_pair(primary, paper, allow_same_database=True)
    assert pair["same_database"] is True
    assert pair["isolation_mode"] == "shared_database_schema"
    assert pair["same_server"] is True
    assert pair["distinct_runtime_roles"] is True
    assert pair["distinct_credentials"] is True


def test_customer_paper_shared_database_mode_rejects_reused_role_or_password():
    primary = "postgresql+asyncpg://atlas_primary:primary_password@db.example:5432/postgres"
    same_role = "postgresql+asyncpg://atlas_primary:paper_password@db.example:5432/postgres"
    same_password = "postgresql+asyncpg://atlas_paper:primary_password@db.example:5432/postgres"

    with pytest.raises(ValueError, match="distinct PostgreSQL runtime role"):
        validate_database_url_pair(primary, same_role, allow_same_database=True)
    with pytest.raises(ValueError, match="distinct database credential"):
        validate_database_url_pair(primary, same_password, allow_same_database=True)


@pytest.mark.postgres
@pytest.mark.skipif(
    not (os.getenv("ATLAS_PAPER_SHARED_PRIMARY_TEST_DATABASE_URL") and os.getenv("ATLAS_PAPER_TEST_DATABASE_URL")),
    reason="requires the disposable PostgreSQL paper schema and separate runtime role provisioned by CI",
)
def test_customer_paper_shared_database_schema_preflight_isolated_from_primary_relations(monkeypatch):
    primary_url = os.environ["ATLAS_PAPER_SHARED_PRIMARY_TEST_DATABASE_URL"]
    paper_url = os.environ["ATLAS_PAPER_TEST_DATABASE_URL"]
    pair = validate_database_url_pair(primary_url, paper_url, allow_same_database=True)
    assert pair["same_database"] is True

    monkeypatch.setattr(settings, "database_url", primary_url)
    monkeypatch.setattr(settings, "customer_paper_database_url", paper_url)
    monkeypatch.setattr(settings, "customer_paper_ledger_enabled", True)
    monkeypatch.setattr(settings, "customer_paper_shared_database_enabled", True)

    result = asyncio.run(preflight_isolated_customer_paper_ledger())
    assert result["status"] == "PASS", result
    assert result["isolation_mode"] == "shared_database_schema"
    assert result["same_database_schema_isolation"] is True
    assert result["enables_customer_paper_trading"] is False


def test_customer_paper_database_pair_allows_distinct_databases_on_same_server():
    primary = "postgresql+asyncpg://atlas_primary:primary_password@db.example:5432/atlas"
    paper = "postgresql+asyncpg://atlas_paper:paper_password@db.example:5432/atlas_paper"
    pair = validate_database_url_pair(primary, paper)
    assert pair["primary_database"] == "atlas"
    assert pair["paper_database"] == "atlas_paper"
    assert pair["same_server"] is True
    assert pair["distinct_runtime_roles"] is True
    assert pair["distinct_credentials"] is True


def test_customer_paper_database_pair_accepts_distinct_database_clusters():
    primary = "postgresql+asyncpg://atlas_primary:primary_password@primary.example:5432/atlas"
    paper = "postgresql+asyncpg://atlas_paper:paper_password@paper.example:5432/atlas_paper"
    pair = validate_database_url_pair(primary, paper)
    assert pair["same_server"] is False
    assert pair["primary_host"] == "primary.example"
    assert pair["paper_host"] == "paper.example"
    assert pair["distinct_runtime_roles"] is True
    assert pair["distinct_credentials"] is True


def test_primary_security_definer_scan_excludes_only_shared_paper_schema():
    source = Path("app/customer_paper_ledger.py").read_text(encoding="utf-8")
    # In shared-database mode the customer_paper schema is the isolated paper boundary.
    # Its guard helper is checked independently in the paper connection; primary schemas
    # must still be scanned for executable SECURITY DEFINER functions.
    assert "AND (NOT :same_database OR n.nspname <> 'customer_paper')" in source
    assert 'AND p.prosecdef' in source
    assert "has_function_privilege(r.oid, p.oid, 'EXECUTE')" in source



def test_primary_database_preflight_checks_only_relations_reachable_through_schema_usage():
    source = Path("app/customer_paper_ledger.py").read_text(encoding="utf-8")
    # Supabase grants PUBLIC SELECT on extensions.pg_stat_statements views, but the
    # paper role has no USAGE on the extensions schema. These object ACLs alone are
    # not reachable by that role and must not make shared-schema isolation fail.
    assert source.count("AND has_schema_privilege(r.oid, n.oid, 'USAGE')") == 3
    assert "has_table_privilege(r.oid, c.oid, 'SELECT') AS can_select" in source
    assert "has_sequence_privilege(r.oid, c.oid, 'USAGE') AS can_use" in source
    assert "has_function_privilege(r.oid, p.oid, 'EXECUTE')" in source


def test_primary_privilege_preflight_tolerates_paper_role_absent_from_primary_cluster():
    source = Path("app/customer_paper_ledger.py").read_text(encoding="utf-8")
    # Roles are cluster-local. Every primary-database ACL probe must join pg_roles
    # before calling a has_*_privilege function, so an absent cross-cluster principal
    # produces no rows rather than raising PostgreSQL's "role does not exist" error.
    assert source.count("JOIN pg_roles r ON r.rolname = CAST(:paper_role AS name)") == 4
    assert "has_schema_privilege(CAST(:paper_role AS name)" not in source
    assert "has_table_privilege(CAST(:paper_role AS name)" not in source
    assert "has_sequence_privilege(CAST(:paper_role AS name)" not in source
    assert "has_function_privilege(CAST(:paper_role AS name)" not in source
    assert "WHERE r.rolname=current_user), \"" in source
    assert "m.roleid=(SELECT oid FROM pg_roles WHERE rolname=current_user)" in source
    assert "grantor_role.rolname='supabase_admin'" in source
    assert "to_jsonb(m) ? 'inherit_option'" in source
    assert "to_jsonb(m) ? 'set_option'" in source


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

        # This writer is intentionally limited to positive seed journals. Simulated
        # fills must use submit_customer_paper_order/record_customer_paper_fill so
        # cash, positions, fees, and lifecycle events commit atomically.
        balances = await get_customer_paper_balances(customer_id=91001)
        indexed = {(row["currency"], row["account_code"]): Decimal(row["balance"]) for row in balances}
        assert indexed[("USDT", "PAPER_CASH")] == Decimal("100")
        assert indexed[("USDT", "PAPER_EQUITY")] == Decimal("-100")
        assert indexed.get(("USDT", "PAPER_CLEARING"), Decimal("0")) == Decimal("0")

        # RLS hides every account and journal line belonging to a different tenant.
        assert await get_customer_paper_balances(customer_id=91002) == []

    asyncio.run(run())


def test_customer_paper_rls_preflight_accepts_only_exact_tenant_predicate():
    exact = (
        "(customer_id = NULLIF(current_setting('app.customer_id'::text, true), "
        "''::text)::bigint)"
    )
    assert _tenant_policy_expression_is_exact(exact) is True
    # PostgreSQL may render the cast input with redundant parentheses:
    # customer_id = (NULLIF(...))::bigint. This is the same tenant predicate.
    deparsed = (
        "(customer_id = (NULLIF(current_setting('app.customer_id'::text, true), "
        "''::text))::bigint)"
    )
    assert _tenant_policy_expression_is_exact(deparsed) is True
    # Parentheses tolerance must not admit a widened predicate.
    assert _tenant_policy_expression_is_exact(
        deparsed[:-1] + " OR true)"
    ) is False
    assert _tenant_policy_expression_is_exact(
        exact[:-1] + " OR true)"
    ) is False
    assert _tenant_policy_expression_is_exact(
        "(customer_id = NULLIF(current_setting('app.other_id'::text, true), ''::text)::bigint)"
    ) is False
    assert _tenant_policy_expression_is_exact("") is False


def test_customer_paper_policy_validation_checks_permissive_mode_and_all_predicates():
    predicate = (
        "(customer_id = NULLIF(current_setting('app.customer_id'::text, true), "
        "''::text)::bigint)"
    )
    policy = {
        "policyname": "customer_paper_tenant",
        "permissive": "PERMISSIVE",
        "cmd": "ALL",
        "qual": predicate,
        "with_check": predicate,
        "roles": ["atlas_paper_app"],
    }
    assert _tenant_policy_validation_error(policy, "atlas_paper_app") is None

    restrictive = {**policy, "permissive": "RESTRICTIVE"}
    assert _tenant_policy_validation_error(restrictive, "atlas_paper_app") == "NOT_PERMISSIVE"

    widened = {**policy, "qual": predicate[:-1] + " OR true)"}
    assert _tenant_policy_validation_error(widened, "atlas_paper_app") == "USING_EXPRESSION"

    broader_role = {**policy, "roles": ["atlas_paper_app", "PUBLIC"]}
    assert _tenant_policy_validation_error(broader_role, "atlas_paper_app") == "ROLE"

    wrong_command = {**policy, "cmd": "SELECT"}
    assert _tenant_policy_validation_error(wrong_command, "atlas_paper_app") == "COMMAND"



@pytest.mark.postgres
@pytest.mark.skipif(
    not (os.getenv("ATLAS_PAPER_TEST_DATABASE_URL") and os.getenv("ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL")),
    reason="requires the isolated disposable PostgreSQL customer paper databases provisioned by CI",
)
def test_customer_paper_numeric_format_supports_postgres_numeric_precision():
    from decimal import Decimal

    from app.customer_paper_lifecycle import _fmt

    assert _fmt(Decimal("99999999999999999999.123456789012345678")) == (
        "99999999999999999999.123456789012345678"
    )


def test_customer_paper_lifecycle_rejects_derivatives_and_mismatched_quote_currency():
    import asyncio

    from app.customer_paper_lifecycle import submit_customer_paper_order

    async def run():
        with pytest.raises(ValueError, match="spot only"):
            await submit_customer_paper_order(
                customer_id=92099,
                client_order_id="unsupported-swap",
                venue="binance",
                market_type="swap",
                symbol="BTC/USDT",
                side="BUY",
                quantity="1",
                currency="USDT",
            )
        with pytest.raises(ValueError, match="QUOTE must match"):
            await submit_customer_paper_order(
                customer_id=92099,
                client_order_id="mismatched-quote",
                venue="binance",
                market_type="spot",
                symbol="BTC/BTC",
                side="BUY",
                quantity="1",
                currency="USDT",
            )

    asyncio.run(run())


def test_customer_paper_lifecycle_is_atomic_idempotent_and_recoverable(monkeypatch):
    customer_id = 92001
    monkeypatch.setattr(settings, "database_url", os.environ["ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL"])
    monkeypatch.setattr(settings, "customer_paper_database_url", os.environ["ATLAS_PAPER_TEST_DATABASE_URL"])
    monkeypatch.setattr(settings, "customer_paper_ledger_enabled", True)
    # This test targets immutable lifecycle/recovery semantics. The separate discipline
    # regression below verifies rate limits with the configured controls enabled.
    monkeypatch.setattr(settings, "discipline_enabled", False)

    async def run():
        await post_customer_paper_journal(
            customer_id=customer_id,
            idempotency_key="lifecycle-seed-92001",
            event_type="PAPER_SEED",
            currency="USDT",
            lines=_seed_lines("1000"),
            reference="disposable-lifecycle-test-seed",
            metadata={"environment": "disposable-ci"},
        )

        # The database-level deferred trigger rejects a balanced journal that tries
        # to reduce cash through a forged PAPER_SEED, bypassing the Python writer.
        from sqlalchemy import text

        engine = paper_lifecycle._paper_engine()
        try:
            account_rows = await get_customer_paper_balances(customer_id=customer_id)
            assert {row["account_code"] for row in account_rows} >= {"PAPER_CASH", "PAPER_EQUITY"}
            with pytest.raises(Exception, match="PAPER_SEED must add positive PAPER_CASH"):
                async with engine.begin() as conn:
                    await conn.execute(
                        text("SELECT set_config('app.customer_id', :customer_id, true)"),
                        {"customer_id": str(customer_id)},
                    )
                    accounts = (await conn.execute(text(
                        """
                        SELECT account_code, id FROM customer_paper.accounts
                        WHERE customer_id=:customer_id AND currency='USDT'
                          AND account_code IN ('PAPER_CASH','PAPER_EQUITY')
                        """
                    ), {"customer_id": customer_id})).mappings().all()
                    account_ids = {row["account_code"]: row["id"] for row in accounts}
                    journal_id = "92001000-0000-4000-8000-000000000001"
                    await conn.execute(text(
                        """
                        INSERT INTO customer_paper.journals
                            (id, customer_id, idempotency_key, event_type, reference, metadata_json)
                        VALUES
                            (:id, :customer_id, 'forged-negative-seed-92001', 'PAPER_SEED',
                             'negative-seed-database-guard', '{}'::jsonb)
                        """
                    ), {"id": journal_id, "customer_id": customer_id})
                    await conn.execute(text(
                        """
                        INSERT INTO customer_paper.journal_lines
                            (journal_id, account_id, customer_id, currency, signed_amount)
                        VALUES
                            (:journal_id, :cash_account, :customer_id, 'USDT', -1),
                            (:journal_id, :equity_account, :customer_id, 'USDT', 1)
                        """
                    ), {
                        "journal_id": journal_id, "customer_id": customer_id,
                        "cash_account": account_ids["PAPER_CASH"],
                        "equity_account": account_ids["PAPER_EQUITY"],
                    })
        finally:
            await engine.dispose()

        order = await submit_customer_paper_order(
            customer_id=customer_id,
            client_order_id="lifecycle-btc-buy-92001",
            venue="binance",
            market_type="spot",
            symbol="BTC/USDT",
            side="BUY",
            quantity="2",
            currency="USDT",
            order_type="LIMIT",
            limit_price="100",
        )
        assert order["status"] == "APPENDED"

        # Price validation and a fault injected after the fill insert both fail
        # inside the transaction; neither is allowed to leave a partial fill.
        with pytest.raises(CustomerPaperOrderConflict, match="limit price"):
            await record_customer_paper_fill(
                customer_id=customer_id,
                client_order_id="lifecycle-btc-buy-92001",
                idempotency_key="lifecycle-fill-1",
                bar_key="btc-bar-1",
                quantity="1",
                price="101",
                fee_amount="1",
            )

        original_append = paper_lifecycle._append_journal_on_connection

        async def fail_after_fill_insert(*args, **kwargs):
            raise RuntimeError("injected journal fault to verify transaction rollback")

        monkeypatch.setattr(paper_lifecycle, "_append_journal_on_connection", fail_after_fill_insert)
        with pytest.raises(RuntimeError, match="injected journal fault"):
            await record_customer_paper_fill(
                customer_id=customer_id,
                client_order_id="lifecycle-btc-buy-92001",
                idempotency_key="lifecycle-fill-1",
                bar_key="btc-bar-1",
                quantity="1",
                price="95",
                fee_amount="1",
            )
        monkeypatch.setattr(paper_lifecycle, "_append_journal_on_connection", original_append)

        recovered = await get_customer_paper_order(
            customer_id=customer_id, client_order_id="lifecycle-btc-buy-92001"
        )
        assert recovered["status"] == "SUBMITTED"
        assert recovered["filled_quantity"] == "0.000000000000000000"

        first = await record_customer_paper_fill(
            customer_id=customer_id,
            client_order_id="lifecycle-btc-buy-92001",
            idempotency_key="lifecycle-fill-1",
            bar_key="btc-bar-1",
            quantity="1",
            price="95",
            fee_amount="1",
        )
        assert first["status"] == "APPENDED"
        assert first["order_status"] == "PARTIALLY_FILLED"
        replay = await record_customer_paper_fill(
            customer_id=customer_id,
            client_order_id="lifecycle-btc-buy-92001",
            idempotency_key="lifecycle-fill-1",
            bar_key="btc-bar-1",
            quantity="1",
            price="95",
            fee_amount="1",
        )
        assert replay["status"] == "REPLAYED"
        with pytest.raises(CustomerPaperIdempotencyConflict, match="different fill payload"):
            await record_customer_paper_fill(
                customer_id=customer_id,
                client_order_id="lifecycle-btc-buy-92001",
                idempotency_key="lifecycle-fill-1",
                bar_key="btc-bar-1",
                quantity="1",
                price="96",
                fee_amount="1",
            )

        second = await record_customer_paper_fill(
            customer_id=customer_id,
            client_order_id="lifecycle-btc-buy-92001",
            idempotency_key="lifecycle-fill-2",
            bar_key="btc-bar-2",
            quantity="1",
            price="100",
            fee_amount="1",
        )
        assert second["order_status"] == "FILLED"
        final_order = await get_customer_paper_order(
            customer_id=customer_id, client_order_id="lifecycle-btc-buy-92001"
        )
        assert final_order["status"] == "FILLED"
        assert final_order["filled_quantity"] == "2.000000000000000000"
        assert await list_open_customer_paper_orders(customer_id=customer_id) == []
        positions = await get_customer_paper_positions(customer_id=customer_id)
        assert len(positions) == 1
        assert positions[0]["symbol"] == "BTC/USDT"
        assert positions[0]["quantity"] == "2.000000000000000000"
        assert positions[0]["cost_basis"] == "195.000000000000000000"

        reversed_fill = await reverse_customer_paper_fill(
            customer_id=customer_id, fill_id=second["fill_id"], idempotency_key="lifecycle-reverse-2"
        )
        assert reversed_fill["status"] == "APPENDED"
        reversal_replay = await reverse_customer_paper_fill(
            customer_id=customer_id, fill_id=second["fill_id"], idempotency_key="lifecycle-reverse-2"
        )
        assert reversal_replay["status"] == "REPLAYED"
        # The most recent position event is now a reversal, so an older fill may not
        # be reversed out of order and silently restate later P&L.
        with pytest.raises(CustomerPaperOrderConflict, match="latest non-reversed fill"):
            await reverse_customer_paper_fill(
                customer_id=customer_id, fill_id=first["fill_id"], idempotency_key="lifecycle-reverse-1"
            )
        positions_after_reversal = await get_customer_paper_positions(customer_id=customer_id)
        assert positions_after_reversal[0]["quantity"] == "1.000000000000000000"
        assert positions_after_reversal[0]["cost_basis"] == "95.000000000000000000"
        balances = await get_customer_paper_balances(customer_id=customer_id)
        by_code = {row["account_code"]: Decimal(row["balance"]) for row in balances}
        assert by_code["PAPER_CASH"] == Decimal("904")
        assert by_code["PAPER_POSITION"] == Decimal("95")
        assert by_code["PAPER_FEES"] == Decimal("1")
        assert by_code["PAPER_EQUITY"] == Decimal("-1000")

        # Queries are tenant-filtered; a different customer cannot inspect this order.
        assert await get_customer_paper_order(
            customer_id=92002, client_order_id="lifecycle-btc-buy-92001"
        ) is None
        with pytest.raises(CustomerPaperOrderConflict, match="does not exist for this tenant"):
            await cancel_customer_paper_order(
                customer_id=92002,
                client_order_id="lifecycle-btc-buy-92001",
                idempotency_key="tenant-cross-cancel",
            )
        with pytest.raises(CustomerPaperOrderConflict, match="state FILLED"):
            await cancel_customer_paper_order(
                customer_id=customer_id,
                client_order_id="lifecycle-btc-buy-92001",
                idempotency_key="cancel-filled-order",
            )

        # The public client-order identifier allows 180 characters while the
        # order-event key column is also bounded. Derive the event key from the
        # fixed-length SHA-256 payload digest so maximum-length IDs remain valid.
        long_client_id = "x" * 180
        long_order = await submit_customer_paper_order(
            customer_id=customer_id,
            client_order_id=long_client_id,
            venue="binance",
            market_type="spot",
            symbol="ETH/USDT",
            side="BUY",
            quantity="1",
            currency="USDT",
            order_type="LIMIT",
            limit_price="100",
        )
        assert long_order["status"] == "APPENDED"
        long_order_replay = await submit_customer_paper_order(
            customer_id=customer_id,
            client_order_id=long_client_id,
            venue="binance",
            market_type="spot",
            symbol="ETH/USDT",
            side="BUY",
            quantity="1",
            currency="USDT",
            order_type="LIMIT",
            limit_price="100",
        )
        assert long_order_replay["status"] == "REPLAYED"

    asyncio.run(run())


@pytest.mark.postgres
@pytest.mark.skipif(
    not (os.getenv("ATLAS_PAPER_TEST_DATABASE_URL") and os.getenv("ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL")),
    reason="requires the isolated disposable PostgreSQL customer paper databases provisioned by CI",
)
def test_customer_paper_cash_is_serialized_across_symbols(monkeypatch):
    # These tests cover ledger lifecycle/cash invariants; discipline is asserted separately.
    monkeypatch.setattr(settings, "discipline_enabled", False)
    customer_id = 92003
    monkeypatch.setattr(settings, "database_url", os.environ["ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL"])
    monkeypatch.setattr(settings, "customer_paper_database_url", os.environ["ATLAS_PAPER_TEST_DATABASE_URL"])
    monkeypatch.setattr(settings, "customer_paper_ledger_enabled", True)

    async def run():
        await post_customer_paper_journal(
            customer_id=customer_id,
            idempotency_key="cross-symbol-seed-92003",
            event_type="PAPER_SEED",
            currency="USDT",
            lines=_seed_lines("100"),
            reference="disposable-cash-lock-test-seed",
            metadata={"environment": "disposable-ci"},
        )
        for symbol, client_id in (("BTC/USDT", "cash-btc-order"), ("ETH/USDT", "cash-eth-order")):
            await submit_customer_paper_order(
                customer_id=customer_id,
                client_order_id=client_id,
                venue="binance",
                market_type="spot",
                symbol=symbol,
                side="BUY",
                quantity="1",
                currency="USDT",
                order_type="LIMIT",
                limit_price="100",
            )

        # The database rejects an order-state event that claims a fill when no
        # corresponding immutable fill exists, even if a buggy caller bypasses helpers.
        from sqlalchemy import text

        order_record = await get_customer_paper_order(
            customer_id=customer_id, client_order_id="cash-btc-order"
        )
        engine = paper_lifecycle._paper_engine()
        try:
            with pytest.raises(Exception, match="filled_quantity must equal the sum of persisted fills"):
                async with engine.begin() as conn:
                    await conn.execute(
                        text("SELECT set_config('app.customer_id', :customer_id, true)"),
                        {"customer_id": str(customer_id)},
                    )
                    await conn.execute(text(
                        """
                        INSERT INTO customer_paper.order_events
                            (customer_id, order_id, event_key, event_type, filled_quantity, metadata_json)
                        VALUES
                            (:customer_id, :order_id, 'tampered-cumulative-fill', 'PARTIALLY_FILLED', 1, '{}'::jsonb)
                        """
                    ), {"customer_id": customer_id, "order_id": order_record["order_id"]})
        finally:
            await engine.dispose()
        unchanged = await get_customer_paper_order(
            customer_id=customer_id, client_order_id="cash-btc-order"
        )
        assert unchanged["status"] == "SUBMITTED"
        assert unchanged["filled_quantity"] == "0.000000000000000000"

        async def try_fill(client_id, bar):
            try:
                return await record_customer_paper_fill(
                    customer_id=customer_id,
                    client_order_id=client_id,
                    idempotency_key="cross-symbol-fill-" + client_id,
                    bar_key=bar,
                    quantity="1",
                    price="90",
                )
            except CustomerPaperOrderConflict as exc:
                return exc

        results = await asyncio.gather(
            try_fill("cash-btc-order", "cash-btc-bar"),
            try_fill("cash-eth-order", "cash-eth-bar"),
        )
        assert sum(isinstance(result, dict) and result["status"] == "APPENDED" for result in results) == 1
        assert sum(isinstance(result, CustomerPaperOrderConflict) for result in results) == 1
        balances = await get_customer_paper_balances(customer_id=customer_id)
        by_code = {row["account_code"]: Decimal(row["balance"]) for row in balances}
        assert by_code["PAPER_CASH"] == Decimal("10")
        positions = await get_customer_paper_positions(customer_id=customer_id)
        assert len(positions) == 1
        assert positions[0]["quantity"] == "1.000000000000000000"

    asyncio.run(run())



@pytest.mark.postgres
@pytest.mark.skipif(
    not (os.getenv("ATLAS_PAPER_TEST_DATABASE_URL") and os.getenv("ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL")),
    reason="requires the isolated disposable PostgreSQL customer paper databases provisioned by CI",
)
def test_customer_paper_sell_pnl_terminal_events_and_reversal_cash_guard(monkeypatch):
    # These tests cover ledger lifecycle/cash invariants; discipline is asserted separately.
    monkeypatch.setattr(settings, "discipline_enabled", False)
    customer_id = 92004
    monkeypatch.setattr(settings, "database_url", os.environ["ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL"])
    monkeypatch.setattr(settings, "customer_paper_database_url", os.environ["ATLAS_PAPER_TEST_DATABASE_URL"])
    monkeypatch.setattr(settings, "customer_paper_ledger_enabled", True)

    async def submit_spot_order(client_order_id, symbol, side, limit_price):
        return await submit_customer_paper_order(
            customer_id=customer_id,
            client_order_id=client_order_id,
            venue="binance",
            market_type="spot",
            symbol=symbol,
            side=side,
            quantity="1",
            currency="USDT",
            order_type="LIMIT",
            limit_price=limit_price,
        )

    async def run():
        await post_customer_paper_journal(
            customer_id=customer_id,
            idempotency_key="sell-seed-92004",
            event_type="PAPER_SEED",
            currency="USDT",
            lines=_seed_lines("100"),
            reference="disposable-sell-test-seed",
            metadata={"environment": "disposable-ci"},
        )
        await submit_spot_order("eth-buy-92004", "ETH/USDT", "BUY", "100")
        await record_customer_paper_fill(
            customer_id=customer_id,
            client_order_id="eth-buy-92004",
            idempotency_key="eth-buy-fill-92004",
            bar_key="eth-bar-buy",
            quantity="1",
            price="90",
        )
        await submit_spot_order("eth-sell-92004", "ETH/USDT", "SELL", "95")
        sell = await record_customer_paper_fill(
            customer_id=customer_id,
            client_order_id="eth-sell-92004",
            idempotency_key="eth-sell-fill-92004",
            bar_key="eth-bar-sell",
            quantity="1",
            price="95",
            fee_amount="1",
        )
        assert sell["realized_pnl"] == "5.000000000000000000"

        # Cash from ETH sale is subsequently spent on BTC. Reversing that sale
        # would debit more cash than remains and must not mutate any append-only rows.
        await submit_spot_order("btc-buy-92004", "BTC/USDT", "BUY", "100")
        await record_customer_paper_fill(
            customer_id=customer_id,
            client_order_id="btc-buy-92004",
            idempotency_key="btc-buy-fill-92004",
            bar_key="btc-bar-buy",
            quantity="1",
            price="100",
        )
        with pytest.raises(CustomerPaperOrderConflict, match="overdraw paper cash"):
            await reverse_customer_paper_fill(
                customer_id=customer_id,
                fill_id=sell["fill_id"],
                idempotency_key="reverse-eth-sale-after-spend",
            )
        balances = await get_customer_paper_balances(customer_id=customer_id)
        by_code = {row["account_code"]: Decimal(row["balance"]) for row in balances}
        assert by_code["PAPER_CASH"] == Decimal("4")
        assert by_code["PAPER_POSITION"] == Decimal("100")
        assert by_code["PAPER_FEES"] == Decimal("1")
        assert by_code["PAPER_PNL"] == Decimal("-5")
        assert by_code["PAPER_EQUITY"] == Decimal("-100")

        await submit_spot_order("sol-cancel-92004", "SOL/USDT", "BUY", "10")
        cancelled = await cancel_customer_paper_order(
            customer_id=customer_id,
            client_order_id="sol-cancel-92004",
            idempotency_key="sol-cancel-key",
        )
        assert cancelled["state"] == "CANCELLED"
        replay_cancel = await cancel_customer_paper_order(
            customer_id=customer_id,
            client_order_id="sol-cancel-92004",
            idempotency_key="sol-cancel-key",
        )
        assert replay_cancel["status"] == "REPLAYED"
        with pytest.raises(CustomerPaperOrderConflict, match="state CANCELLED"):
            await record_customer_paper_fill(
                customer_id=customer_id,
                client_order_id="sol-cancel-92004",
                idempotency_key="fill-cancelled-sol",
                bar_key="sol-after-cancel",
                quantity="1",
                price="9",
            )

        await submit_spot_order("xrp-expire-92004", "XRP/USDT", "BUY", "10")
        expired = await expire_customer_paper_order(
            customer_id=customer_id,
            client_order_id="xrp-expire-92004",
            idempotency_key="xrp-expiry-key",
        )
        assert expired["state"] == "EXPIRED"
        replay_expiry = await expire_customer_paper_order(
            customer_id=customer_id,
            client_order_id="xrp-expire-92004",
            idempotency_key="xrp-expiry-key",
        )
        assert replay_expiry["status"] == "REPLAYED"
        assert (await get_customer_paper_order(
            customer_id=customer_id, client_order_id="xrp-expire-92004"
        ))["status"] == "EXPIRED"
        assert await list_open_customer_paper_orders(customer_id=customer_id) == []
        positions = await get_customer_paper_positions(customer_id=customer_id)
        assert len(positions) == 1 and positions[0]["symbol"] == "BTC/USDT"

    asyncio.run(run())



def test_pooler_login_is_normalized_to_role_only_for_configured_project(monkeypatch):
    project_ref = "wauaelzyugjuepmrewaw"
    monkeypatch.setattr(
        settings, "supabase_url", f"https://{project_ref}.supabase.co"
    )
    pooler = (
        "postgresql+asyncpg://atlas_customer_paper."
        f"{project_ref}:private-password@aws-0-eu-west-1.pooler.supabase.com:5432/"
        "postgres?ssl=require"
    )
    assert _configured_database_role(pooler) == "atlas_customer_paper"

    direct = (
        f"postgresql+asyncpg://atlas_customer_paper:private-password@db.{project_ref}."
        "supabase.co:5432/postgres?ssl=require"
    )
    assert _configured_database_role(direct) == "atlas_customer_paper"


def test_pooler_login_rejects_wrong_project_suffix(monkeypatch):
    project_ref = "wauaelzyugjuepmrewaw"
    monkeypatch.setattr(
        settings, "supabase_url", f"https://{project_ref}.supabase.co"
    )
    wrong_project_url = (
        "postgresql+asyncpg://atlas_customer_paper.abcdefghijklmnopqrst:"
        "private-password@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?ssl=require"
    )
    with pytest.raises(ValueError, match="configured project reference"):
        _configured_database_role(wrong_project_url)


@pytest.mark.skipif(
    not (os.getenv("ATLAS_PAPER_TEST_DATABASE_URL") and os.getenv("ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL")),
    reason="requires the disposable isolated PostgreSQL paper ledger provisioned by CI",
)
def test_customer_paper_entry_discipline_and_position_ownership_are_enforced(monkeypatch):
    customer_id = 925551
    monkeypatch.setattr(settings, "database_url", os.environ["ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL"])
    monkeypatch.setattr(settings, "customer_paper_database_url", os.environ["ATLAS_PAPER_TEST_DATABASE_URL"])
    monkeypatch.setattr(settings, "customer_paper_ledger_enabled", True)
    monkeypatch.setattr(settings, "discipline_enabled", True)
    monkeypatch.setattr(settings, "discipline_max_entries_per_day", 20)
    monkeypatch.setattr(settings, "discipline_entry_cooldown_seconds", 900)

    async def run():
        await post_customer_paper_journal(
            customer_id=customer_id,
            idempotency_key="discipline-seed-925551",
            event_type="PAPER_SEED",
            currency="USDT",
            lines=_seed_lines("1000"),
            reference="paper-entry-discipline-regression",
            metadata={"environment": "disposable-ci"},
        )
        common = {
            "customer_id": customer_id,
            "venue": "binance",
            "market_type": "spot",
            "currency": "USDT",
            "order_type": "MARKET",
        }
        bot_entry = {
            "paper_flow_version": 1,
            "execution_owner": "autonomous_customer_bot",
            "order_role": "ENTRY",
            "timeframe": "1h",
            "signal_bar_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        first = await submit_customer_paper_order(
            **common, client_order_id="discipline-btc-entry-925551",
            symbol="BTC/USDT", side="BUY", quantity="1", metadata=bot_entry,
        )
        assert first["status"] == "APPENDED"

        with pytest.raises(CustomerPaperOrderConflict, match="cooldown is active"):
            await submit_customer_paper_order(
                **common, client_order_id="discipline-eth-entry-925551",
                symbol="ETH/USDT", side="BUY", quantity="1", metadata=bot_entry,
            )

        # Exercise the daily count independently of cooldown.
        monkeypatch.setattr(settings, "discipline_entry_cooldown_seconds", 0)
        monkeypatch.setattr(settings, "discipline_max_entries_per_day", 1)
        with pytest.raises(CustomerPaperOrderConflict, match="daily entry limit reached"):
            await submit_customer_paper_order(
                **common, client_order_id="discipline-sol-entry-925551",
                symbol="SOL/USDT", side="BUY", quantity="1", metadata=bot_entry,
            )

        fill = await record_customer_paper_fill(
            customer_id=customer_id,
            client_order_id="discipline-btc-entry-925551",
            idempotency_key="discipline-btc-fill-925551",
            bar_key="discipline-btc-bar-925551",
            quantity="1",
            price="100",
            fee_amount="0",
        )
        assert fill["status"] == "APPENDED"

        manual_exit = {
            "execution_owner": "manual_customer_api",
            "order_role": "STRATEGY_EXIT",
            "timeframe": "1h",
        }
        with pytest.raises(CustomerPaperOrderConflict, match="same open position"):
            await submit_customer_paper_order(
                **common, client_order_id="discipline-manual-sell-925551",
                symbol="BTC/USDT", side="SELL", quantity="1", metadata=manual_exit,
            )

        # Entry cooldown/daily caps never block a protective exit from its owning flow.
        protection = await submit_customer_paper_order(
            **common, client_order_id="discipline-protective-exit-925551",
            symbol="BTC/USDT", side="SELL", quantity="1",
            metadata={
                **bot_entry,
                "order_role": "PROTECTIVE_EXIT",
            },
        )
        assert protection["status"] == "APPENDED"

    asyncio.run(run())
