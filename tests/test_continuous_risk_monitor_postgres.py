"""PostgreSQL regression for daily risk-baseline rollover after account inactivity."""
import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("ATLAS_POSTGRES_URL"),
    reason="requires PostgreSQL (ATLAS_POSTGRES_URL)",
)
async def test_idle_account_rolls_daily_risk_baseline_after_multiple_days(monkeypatch):
    """An idle account's old daily loss must not trigger a false halt on restart.

    The account is isolated in a temporary PostgreSQL schema so this test neither
    reads nor mutates other test rows. The loop's normal persistence/commit path is
    exercised against PostgreSQL, with emergency actions replaced by test spies.
    """
    from app import main
    from app.db import AppState, TradingAccount

    url = os.getenv("ATLAS_POSTGRES_URL", "").strip()
    assert url, "ATLAS_POSTGRES_URL is required for this PostgreSQL test"

    schema = "risk_idle_" + uuid.uuid4().hex
    root_engine = create_async_engine(url)
    isolated_engine = None
    task = None
    incidents = []
    stops = []

    async def capture_incident(**kwargs):
        incidents.append(kwargs)

    async def capture_stop():
        stops.append(True)

    try:
        async with root_engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

        isolated_engine = create_async_engine(
            url,
            connect_args={"server_settings": {"search_path": schema}},
        )
        async with isolated_engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: AppState.__table__.create(sync_conn))
            await conn.run_sync(lambda sync_conn: TradingAccount.__table__.create(sync_conn))

        sessions = async_sessionmaker(isolated_engine, expire_on_commit=False)
        today_utc = datetime.now(timezone.utc).date()
        customer_id = 2_000_000_000 - (uuid.uuid4().int % 100_000_000)
        account = TradingAccount(
            customer_id=customer_id,
            currency="USDT",
            status="ACTIVE",
            cash_equity=900.0,
            equity=900.0,
            peak_equity=1000.0,
            daily_start_equity=1000.0,
            daily_start_date=today_utc - timedelta(days=3),
        )
        async with sessions() as db:
            db.add(account)
            await db.commit()
            account_id = account.id

        monkeypatch.setattr(main, "SessionLocal", sessions)
        monkeypatch.setattr(main.settings, "max_drawdown", 0.25)
        monkeypatch.setattr(main.settings, "daily_loss_limit", 0.05)
        monkeypatch.setattr(main.settings, "continuous_risk_check_interval_seconds", 3600)
        monkeypatch.setattr(main, "open_incident", capture_incident)
        monkeypatch.setattr(main, "emergency_stop", capture_stop)

        task = asyncio.create_task(main._continuous_risk_enforcement_loop())
        deadline = asyncio.get_running_loop().time() + 5.0
        observed = None
        while asyncio.get_running_loop().time() < deadline:
            async with sessions() as db:
                observed = await db.get(TradingAccount, account_id)
                if observed is not None and observed.daily_start_date == today_utc:
                    break
            await asyncio.sleep(0.01)

        assert observed is not None
        assert observed.daily_start_date == today_utc, "multi-day stale baseline was not persisted"
        assert observed.daily_start_equity == pytest.approx(900.0)
        assert observed.status == "ACTIVE", "stale daily loss incorrectly halted an otherwise safe account"
        assert incidents == [], "risk monitor reported a breach after rolling the daily baseline"
        assert stops == [], "risk monitor called emergency stop despite the daily-baseline rollover"
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if isolated_engine is not None:
            await isolated_engine.dispose()
        async with root_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await root_engine.dispose()
