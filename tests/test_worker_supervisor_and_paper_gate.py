"""Behavioral regressions for current paper gate and worker supervisor."""
import asyncio
import uuid
from types import SimpleNamespace

import pytest


def test_supervisor_restarts_worker_after_audit_failure(monkeypatch):
    import app.main as main

    worker_name = f"supervisor-regression-{uuid.uuid4().hex}"
    calls = []

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", no_wait)
    monkeypatch.setitem(main._worker_loop_health, worker_name, {"restart_count": 0})

    async def broken_audit(*args, **kwargs):
        raise RuntimeError("audit database unavailable")

    monkeypatch.setattr(main, "_audit", broken_audit)

    async def worker():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            await main._audit("WORKER_TEST_FAILURE", {"cycle": 1})
        raise asyncio.CancelledError()

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await main._supervise_worker_loop(worker, worker_name)

    asyncio.run(run())

    assert calls == [1, 2]
    assert main._worker_loop_health[worker_name]["restart_count"] == 1
    assert main._worker_loop_health[worker_name]["status"] == "STOPPED"


def test_customer_forex_paper_cycle_stops_before_market_data_or_ai(monkeypatch):
    import app.main as main

    bot = SimpleNamespace(
        id=77, customer_id=12, trading_account_id=31, asset="forex",
        symbol="EUR_USD", exchange="oanda", timeframe="1h", days=365,
        risk_fraction=0.01, interval_seconds=900, mode="PAPER",
        status="RUNNING", next_run_at=None, last_error="",
    )
    profile = SimpleNamespace(id=12, status="ACTIVE")
    account = SimpleNamespace(id=31, status="ACTIVE", equity=10000.0)
    state = SimpleNamespace(kill_switch=False, live_enabled=False)

    class Result:
        def scalar_one_or_none(self):
            return bot

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, _query):
            return Result()

        async def get(self, model, _ident):
            return {
                "CustomerProfile": profile,
                "TradingAccount": account,
                "AppState": state,
            }.get(model.__name__)

        async def commit(self):
            return None

    monkeypatch.setattr(main, "SessionLocal", lambda: FakeDB())
    monkeypatch.setattr(main.settings, "paper_trading", True)
    monkeypatch.setattr(main.settings, "live_trading_enabled", False)

    calls = {"market_data": 0, "ai": 0}

    async def forbidden_data(*args, **kwargs):
        calls["market_data"] += 1
        raise AssertionError("market data must not run before the isolated-ledger gate")

    async def forbidden_ai(*args, **kwargs):
        calls["ai"] += 1
        raise AssertionError("AI must not run before the isolated-ledger gate")

    monkeypatch.setattr(main, "_fetch_customer_oanda_data", forbidden_data)
    monkeypatch.setattr(main, "_ensure_adaptive_model_locked", forbidden_ai)

    result = asyncio.run(main._run_persisted_adaptive_bot_cycle(
        77,
        main.CustomerBotStartRequest(
            asset="forex", symbol="EUR_USD", exchange="oanda",
            timeframe="1h", days=365, autonomous=False,
        ),
        decision_id="test-current-main-paper-gate",
    ))

    assert result["decision"] == "NO_TRADE"
    assert result["stage"] == "simulation_ledger_required"
    assert bot.status == "STOPPED"
    assert bot.next_run_at is None
    assert calls == {"market_data": 0, "ai": 0}
