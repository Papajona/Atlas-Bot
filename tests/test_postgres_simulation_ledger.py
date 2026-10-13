"""Database-backed isolation tests for the virtual customer simulation ledger.

Requires PostgreSQL after `alembic upgrade head`; the required workflow must not
silently skip these tests.
"""
import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.postgres


def _pg_url():
    for name in ("ATLAS_POSTGRES_URL", "DATABASE_URL"):
        value = os.getenv(name, "").strip()
        if value.startswith("postgresql"):
            return value
    pytest.skip("Set ATLAS_POSTGRES_URL/DATABASE_URL to migrated PostgreSQL")


async def _setup():
    engine = create_async_engine(
        _pg_url(),
        pool_size=30,
        max_overflow=0,
        connect_args={"statement_cache_size": 0, "server_settings": {"lock_timeout": "20000"}},
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _customer(sessions):
    token = uuid.uuid4().hex
    async with sessions() as db:
        async with db.begin():
            result = await db.execute(text("""
                INSERT INTO customer_profiles (auth_user_id, email, display_name, status)
                VALUES (:auth, :email, :name, 'ACTIVE') RETURNING id
            """), {"auth": f"sim-drill-{token}", "email": f"sim-{token}@example.invalid",
                   "name": "simulation isolation test"})
            return int(result.scalar_one())


async def _real_money_snapshot(sessions, customer_id):
    async with sessions() as db:
        ledger = (await db.execute(text("""
            SELECT available, trading_reserved, withdrawal_reserved
            FROM customer_ledger_accounts WHERE customer_id=:cid
        """), {"cid": customer_id})).all()
        wallets = (await db.execute(text("""
            SELECT id, balance, updated_at FROM wallets WHERE customer_id=:cid ORDER BY id
        """), {"cid": customer_id})).all()
        journals = (await db.execute(text("""
            SELECT count(*) FROM ledger_journals WHERE customer_id=:cid
        """), {"cid": customer_id})).scalar_one()
        lines = (await db.execute(text("""
            SELECT count(*) FROM ledger_journal_lines WHERE customer_id=:cid
        """), {"cid": customer_id})).scalar_one()
        return (ledger, wallets, journals, lines)


@pytest.mark.asyncio
async def test_concurrent_duplicate_events_apply_balance_delta_once():
    from app.simulation_ledger import create_simulation_account, post_simulation_event
    engine, sessions = await _setup()
    try:
        cid = await _customer(sessions)
        account_id = await create_simulation_account(cid, "forex", initial_virtual_balance="10000", daily_loss_limit="500")

        async def post():
            return await post_simulation_event(
                customer_id=cid, account_id=account_id, idempotency_key="same-fill",
                event_type="ORDER_FILL", balance_delta="-125", asset_class="forex", simulation_mode="SIMULATION",
                symbol="EUR_USD", quantity="1", price="1.08",
            )
        results = await asyncio.gather(*(post() for _ in range(16)))
        assert results == [Decimal("9875")] * 16
        async with sessions() as db:
            balance = (await db.execute(text("""
                SELECT virtual_balance FROM customer_simulation_accounts WHERE id=:id
            """), {"id": account_id})).scalar_one()
            count = (await db.execute(text("""
                SELECT count(*) FROM customer_simulation_events
                WHERE account_id=:id AND idempotency_key='same-fill'
            """), {"id": account_id})).scalar_one()
        assert Decimal(balance) == Decimal("9875")
        assert count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_loss_events_cannot_exceed_daily_loss_cap():
    from app.simulation_ledger import SimulationLedgerError, create_simulation_account, post_simulation_event
    engine, sessions = await _setup()
    try:
        cid = await _customer(sessions)
        account_id = await create_simulation_account(cid, "commodity", initial_virtual_balance="10000", daily_loss_limit="100")

        async def loss(i):
            try:
                return await post_simulation_event(
                    customer_id=cid, account_id=account_id, idempotency_key=f"loss-{i}",
                    event_type="REALIZED_PNL", balance_delta="-60", asset_class="commodity", simulation_mode="SIMULATION",
                    symbol="XAUUSD",
                )
            except SimulationLedgerError:
                return "blocked"
        results = await asyncio.gather(*(loss(i) for i in range(8)))
        assert sum(1 for value in results if value != "blocked") == 1
        async with sessions() as db:
            losses = (await db.execute(text("""
                SELECT COALESCE(SUM(-balance_delta), 0)
                FROM customer_simulation_events
                WHERE account_id=:id AND event_type='REALIZED_PNL' AND balance_delta < 0
            """), {"id": account_id})).scalar_one()
            balance = (await db.execute(text("""
                SELECT virtual_balance FROM customer_simulation_accounts WHERE id=:id
            """), {"id": account_id})).scalar_one()
        assert Decimal(losses) == Decimal("60")
        assert Decimal(balance) == Decimal("9940")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_simulation_event_rolls_back_event_and_balance():
    from app.simulation_ledger import SimulationLedgerError, create_simulation_account, post_simulation_event
    engine, sessions = await _setup()
    try:
        cid = await _customer(sessions)
        account_id = await create_simulation_account(cid, "forex", initial_virtual_balance="100", daily_loss_limit="50")
        with pytest.raises(SimulationLedgerError, match="negative"):
            await post_simulation_event(
                customer_id=cid, account_id=account_id, idempotency_key="too-large-loss",
                event_type="ORDER_FILL", balance_delta="-101", asset_class="forex",
            )
        async with sessions() as db:
            balance = (await db.execute(text("""
                SELECT virtual_balance FROM customer_simulation_accounts WHERE id=:id
            """), {"id": account_id})).scalar_one()
            count = (await db.execute(text("""
                SELECT count(*) FROM customer_simulation_events
                WHERE account_id=:id AND idempotency_key='too-large-loss'
            """), {"id": account_id})).scalar_one()
        assert Decimal(balance) == Decimal("100")
        assert count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_simulation_activity_cannot_change_real_customer_funds_or_journals():
    from app.simulation_ledger import create_simulation_account, post_simulation_event
    engine, sessions = await _setup()
    try:
        cid = await _customer(sessions)
        before = await _real_money_snapshot(sessions, cid)
        account_id = await create_simulation_account(cid, "crypto", initial_virtual_balance="5000", daily_loss_limit="200")
        await post_simulation_event(
            customer_id=cid, account_id=account_id, idempotency_key="virtual-fill",
            event_type="ORDER_FILL", balance_delta="-25", asset_class="crypto", simulation_mode="SIMULATION",
            symbol="BTCUSDT", quantity="0.001", price="25000",
        )
        await post_simulation_event(
            customer_id=cid, account_id=account_id, idempotency_key="virtual-stop",
            event_type="STOP_LOSS", balance_delta="-10", asset_class="crypto", symbol="BTCUSDT",
        )
        await post_simulation_event(
            customer_id=cid, account_id=account_id, idempotency_key="virtual-cancel",
            event_type="ORDER_CANCEL", balance_delta="0", asset_class="crypto", symbol="BTCUSDT",
        )
        after = await _real_money_snapshot(sessions, cid)
        assert after == before, "Simulation mutated a real customer-funds table"
    finally:
        await engine.dispose()
