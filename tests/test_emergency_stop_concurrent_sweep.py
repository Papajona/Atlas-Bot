"""The kill switch must sweep customer exchange accounts concurrently (bounded), without losing any
per-account verification or incident."""
import asyncio
import threading
import time
import uuid

import pytest
from sqlalchemy import func, select

import app.execution as execution
from app.config import settings
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import AppState, Base, CustomerBinanceAccount


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")
    monkeypatch.setattr(settings, "customer_live_trading_enabled", True)
    monkeypatch.setattr(settings, "emergency_stop_concurrency", 5)


def test_emergency_stop_sweeps_customers_in_bounded_parallel_and_reports_every_failure(monkeypatch, tmp_path):
    TOTAL_MINE, FAIL_EVERY = 14, 4          # accounts 0,4,8,12 fail to cancel
    lock = threading.Lock()
    live = {"now": 0, "max": 0}
    swept: set[int] = set()
    incidents: list[str] = []
    base = int(time.time()) % 1_000_000 * 100

    class FakeBroker:
        def __init__(self, cid, fails):
            self.cid, self.fails = cid, fails

        def cancel_all_orders(self):
            with lock:
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])
            try:
                time.sleep(0.05)             # blocking exchange round trip (runs in a worker thread)
                if self.fails:
                    raise RuntimeError("exchange timeout")
                with lock:
                    swept.add(self.cid)
                return [{"id": f"o-{self.cid}"}]
            finally:
                with lock:
                    live["now"] -= 1

        def fetch_open_orders(self):
            return []

    mine = {base + i: (i % FAIL_EVERY == 0) for i in range(TOTAL_MINE)}
    monkeypatch.setattr(execution, "build_customer_binance_broker",
                        lambda account, **kw: FakeBroker(account.customer_id, mine.get(account.customer_id, False)))

    async def _barrier_ok(_timeout):
        return True
    monkeypatch.setattr(execution, "_wait_for_live_submission_barrier", _barrier_ok)

    async def _record_incident(*, key, **kw):
        incidents.append(key)
    monkeypatch.setattr(execution, "open_incident", _record_incident)

    async def go():
        # emergency_stop() really flips the kill switch. Run it against a private database so this test can
        # never leave the shared test DB halted (it previously made later tests fail with "Kill switch is active").
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kill.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(execution, "SessionLocal", SessionLocal)
        async with SessionLocal() as db:
            db.add(AppState(cash_equity=1000.0, equity=1000.0, peak_equity=1000.0, daily_start_equity=1000.0))  # daily_start_date defaults to today
            for cid in mine:
                db.add(CustomerBinanceAccount(customer_id=cid, subaccount_id=f"sub-{uuid.uuid4().hex}", api_key="k",
                                              can_trade=True, status="VERIFIED"))
            await db.commit()
            total_verified = (await db.execute(select(func.count()).select_from(CustomerBinanceAccount).where(
                CustomerBinanceAccount.status == "VERIFIED", CustomerBinanceAccount.can_trade.is_(True)))).scalar_one()
        try:
            return total_verified, await execution.emergency_stop(None)
        finally:
            await engine.dispose()

    total_verified, result = asyncio.run(go())
    expected_failures = sum(1 for f in mine.values() if f)
    assert result["failure_count"] == expected_failures                # every failing account is reported...
    assert result["ok"] is False and result["broker_halt_confirmed"] is False   # ...and the halt is never reported clean
    assert sum(1 for k in incidents if k.startswith("EMERGENCY_CANCEL_FAILED:CUSTOMER:")) == expected_failures
    assert swept == {cid for cid, f in mine.items() if not f}          # every healthy account was swept exactly once
    assert result["canceled_count"] == len(swept)
    assert 1 < live["max"] <= 5, live                                  # parallel, never above the configured bound
