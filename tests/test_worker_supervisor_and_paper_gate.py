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

@pytest.mark.parametrize(
    "risk_fraction",
    [float("nan"), float("inf"), float("-inf"), 0.0, -0.01, 0.020001],
)
def test_persisted_bot_invalid_risk_fraction_stops_before_market_data_or_ai(monkeypatch, risk_fraction):
    import app.main as main

    bot = SimpleNamespace(
        id=88, customer_id=13, trading_account_id=32, asset="forex",
        symbol="EUR_USD", exchange="oanda", timeframe="1h", days=365,
        risk_fraction=risk_fraction, interval_seconds=900, mode="PAPER",
        status="RUNNING", next_run_at=None, last_error="",
        last_decision="", last_stage="",
    )
    profile = SimpleNamespace(id=13, status="ACTIVE")
    account = SimpleNamespace(id=32, status="ACTIVE", equity=10000.0)
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

    async def forbidden_market_data(*args, **kwargs):
        calls["market_data"] += 1
        raise AssertionError("market data must not be requested for an invalid persisted risk fraction")

    async def forbidden_ai(*args, **kwargs):
        calls["ai"] += 1
        raise AssertionError("AI must not be called for an invalid persisted risk fraction")

    monkeypatch.setattr(main, "_fetch_customer_oanda_data", forbidden_market_data)
    monkeypatch.setattr(main, "_ensure_adaptive_model_locked", forbidden_ai)

    result = asyncio.run(main._run_persisted_adaptive_bot_cycle(
        88,
        main.CustomerBotStartRequest(
            asset="forex", symbol="EUR_USD", exchange="oanda",
            timeframe="1h", days=365, autonomous=False,
        ),
        decision_id="test-invalid-persisted-risk-fraction",
    ))

    assert result["decision"] == "NO_TRADE"
    assert result["stage"] == "risk_fraction_invalid"
    assert bot.status == "STOPPED"
    assert bot.next_run_at is None
    assert bot.last_decision == "NO_TRADE"
    assert bot.last_stage == "risk_fraction_invalid"
    assert "finite value" in bot.last_error
    assert calls == {"market_data": 0, "ai": 0}


@pytest.mark.parametrize("equity", [float("nan"), float("inf"), float("-inf"), 0.0, -1.0])
def test_persisted_live_bot_invalid_equity_stops_before_market_data_or_ai(monkeypatch, equity):
    import app.main as main

    bot = SimpleNamespace(
        id=89, customer_id=14, trading_account_id=33, asset="forex",
        symbol="EUR_USD", exchange="oanda", timeframe="1h", days=365,
        risk_fraction=0.01, interval_seconds=900, mode="LIVE",
        status="RUNNING", next_run_at=None, last_error="",
        last_decision="", last_stage="",
    )
    profile = SimpleNamespace(id=14, status="ACTIVE")
    account = SimpleNamespace(id=33, status="ACTIVE", equity=equity)
    state = SimpleNamespace(kill_switch=False, live_enabled=True)

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
    monkeypatch.setattr(main.settings, "paper_trading", False)
    monkeypatch.setattr(main.settings, "live_trading_enabled", True)
    calls = {"market_data": 0, "ai": 0}

    async def forbidden_market_data(*args, **kwargs):
        calls["market_data"] += 1
        raise AssertionError("market data must not be requested without finite positive live equity")

    async def forbidden_ai(*args, **kwargs):
        calls["ai"] += 1
        raise AssertionError("AI must not be called without finite positive live equity")

    monkeypatch.setattr(main, "_fetch_customer_oanda_data", forbidden_market_data)
    monkeypatch.setattr(main, "_ensure_adaptive_model_locked", forbidden_ai)

    result = asyncio.run(main._run_persisted_adaptive_bot_cycle(
        89,
        main.CustomerBotStartRequest(
            asset="forex", symbol="EUR_USD", exchange="oanda",
            timeframe="1h", days=365, autonomous=False,
        ),
        decision_id="test-invalid-persisted-live-equity",
    ))

    assert result["decision"] == "NO_TRADE"
    assert result["stage"] == "risk_equity_invalid"
    assert bot.status == "STOPPED"
    assert bot.next_run_at is None
    assert bot.last_stage == "risk_equity_invalid"
    assert "finite positive account equity" in bot.last_error
    assert calls == {"market_data": 0, "ai": 0}



@pytest.mark.parametrize("risk_fraction", [float("nan"), 0.0, 0.020001])
def test_supervisor_invalid_persisted_risk_stops_bot_on_first_cycle(monkeypatch, risk_fraction):
    import app.main as main

    bot = SimpleNamespace(
        id=90, customer_id=15, trading_account_id=34, asset="forex",
        symbol="EUR_USD", exchange="oanda", timeframe="1h", days=365,
        risk_fraction=risk_fraction, interval_seconds=900, mode="PAPER",
        status="RUNNING", next_run_at=None, last_error="",
        last_decision="", last_stage="", consecutive_errors=0, last_run_at=None,
    )
    profile = SimpleNamespace(id=15, status="ACTIVE")
    account = SimpleNamespace(id=34, status="ACTIVE", equity=10000.0)
    state = SimpleNamespace(kill_switch=False, live_enabled=False)

    class Result:
        def scalar_one_or_none(self):
            return bot

        def scalars(self):
            return self

        def all(self):
            return [bot]

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
                "AdaptiveTradingBot": bot,
            }.get(model.__name__)

        async def commit(self):
            return None

    async def fake_lock(*args, **kwargs):
        return True

    async def noop(*args, **kwargs):
        return None

    async def forbidden(*args, **kwargs):
        raise AssertionError("market data / AI must not be reached for invalid persisted risk")

    async def stop_after_one_cycle(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(main, "SessionLocal", lambda: FakeDB())
    monkeypatch.setattr(main, "acquire_lock", fake_lock)
    monkeypatch.setattr(main, "refresh_lock", fake_lock)
    monkeypatch.setattr(main, "release_lock", noop)
    monkeypatch.setattr(main, "_record_adaptive_decision_memory", noop)
    monkeypatch.setattr(main, "_fetch_customer_oanda_data", forbidden)
    monkeypatch.setattr(main, "_ensure_adaptive_model_locked", forbidden)
    monkeypatch.setattr(main.settings, "paper_trading", True)
    monkeypatch.setattr(main.settings, "live_trading_enabled", False)
    monkeypatch.setattr(main.asyncio, "sleep", stop_after_one_cycle)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main._adaptive_bot_controller_loop())

    assert bot.status == "STOPPED"
    assert bot.next_run_at is None
    assert bot.last_stage == "risk_fraction_invalid"
    assert bot.last_decision == "NO_TRADE"
    assert "Persisted risk fraction is invalid" in bot.last_error
    assert bot.consecutive_errors == 0


def test_supervisor_preserves_risk_stop_when_decision_recording_raises(monkeypatch):
    import app.main as main

    bot = SimpleNamespace(
        id=91, customer_id=15, trading_account_id=34, asset="forex",
        symbol="EUR_USD", exchange="oanda", timeframe="1h", days=365,
        risk_fraction=float("nan"), interval_seconds=900, mode="PAPER",
        status="RUNNING", next_run_at=None, last_error="", last_decision="",
        last_stage="", consecutive_errors=0, last_run_at=None,
    )
    profile = SimpleNamespace(id=15, status="ACTIVE")
    account = SimpleNamespace(id=34, status="ACTIVE", equity=10000.0)
    state = SimpleNamespace(kill_switch=False, live_enabled=False)

    class Result:
        def scalar_one_or_none(self):
            return bot

        def scalars(self):
            return self

        def all(self):
            return [bot]

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
                "AdaptiveTradingBot": bot,
            }.get(model.__name__)

        async def commit(self):
            return None

    calls = {"record": 0}

    async def fake_record(**_kwargs):
        calls["record"] += 1
        if calls["record"] == 1:
            raise RuntimeError("decision record failed after risk stop")

    async def fake_lock(*_args, **_kwargs):
        return True

    async def noop(*_args, **_kwargs):
        return None

    async def stop_after_one_cycle(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(main, "SessionLocal", lambda: FakeDB())
    monkeypatch.setattr(main, "_record_adaptive_decision_memory", fake_record)
    monkeypatch.setattr(main, "acquire_lock", fake_lock)
    monkeypatch.setattr(main, "refresh_lock", fake_lock)
    monkeypatch.setattr(main, "release_lock", noop)
    monkeypatch.setattr(main.settings, "paper_trading", True)
    monkeypatch.setattr(main.settings, "live_trading_enabled", False)
    monkeypatch.setattr(main.asyncio, "sleep", stop_after_one_cycle)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main._adaptive_bot_controller_loop())

    assert calls["record"] == 2
    assert bot.status == "STOPPED"
    assert bot.next_run_at is None
    assert bot.last_decision == "NO_TRADE"
    assert bot.last_stage == "risk_fraction_invalid"
    assert "Persisted risk fraction is invalid" in bot.last_error
    assert bot.consecutive_errors == 0


def test_daily_research_failed_result_is_audited_as_failure_not_completion(monkeypatch):
    import app.main as main

    sleeps = {"count": 0}
    events = []
    successes = []
    failures = []

    async def controlled_sleep(_seconds):
        sleeps["count"] += 1
        if sleeps["count"] > 1:
            raise asyncio.CancelledError()

    async def failed_research():
        return {
            "status": "FAILED",
            "stage": "distributed_lock",
            "error": "LOCK_LEASE_LOST",
            "symbols_requested": [],
            "runs": [],
            "real_money_execution": False,
        }

    async def safe_audit(event, payload):
        events.append((event, payload))

    monkeypatch.setattr(main.asyncio, "sleep", controlled_sleep)
    monkeypatch.setattr(main, "run_daily_research", failed_research)
    monkeypatch.setattr(main, "_safe_audit", safe_audit)
    monkeypatch.setattr(main, "_mark_worker_loop_success", lambda *args: successes.append(args))
    monkeypatch.setattr(main, "_mark_worker_loop_failure", lambda *args: failures.append(args))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main._daily_research_loop())

    assert successes == []
    assert len(failures) == 1
    assert failures[0][0] == "daily-research"
    assert events == [("RESEARCH_DAILY_FAILED", {
        "status": "FAILED",
        "symbols": [],
        "real_money_execution": False,
        "drift_flags": {},
        "stage": "distributed_lock",
        "error": "LOCK_LEASE_LOST",
    })]
