from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_autonomous_cycle_uses_persisted_risk_fraction():
    src = (ROOT / 'app' / 'main.py').read_text()
    start = src.index('async def _run_persisted_adaptive_bot_cycle')
    end = src.index('async def _daily_research_loop')
    chunk = src[start:end]
    assert 'settings.risk_per_trade if bot.risk_fraction is None else bot.risk_fraction' in chunk
    assert 'not math.isfinite(risk_fraction) or not 0.0 < risk_fraction <= 0.02' in chunk
    assert 'risk_cash = equity * risk_fraction' in chunk
    assert 'risk_fraction_invalid' in chunk

def test_start_path_schedules_next_cycle_after_initial_run():
    src = (ROOT / 'app' / 'main.py').read_text()
    start = src.index('@app.post("/api/customer/bot/start")')
    end = src.index('@app.', start + 10)
    chunk = src[start:end]
    assert 'existing_bot.next_run_at = datetime.now(timezone.utc)' in chunk
    assert 'row.next_run_at = now2 + __import__("datetime").timedelta' in chunk

def test_challenger_cannot_materially_degrade_incumbent():
    src = (ROOT / 'app' / 'adaptive_bot.py').read_text()
    assert 'max_champion_sharpe_drop' in src
    assert 'max_champion_drawdown_worsening' in src
    assert 'max_champion_return_drop' in src
    assert 'WORSE_THAN_CHAMPION' in src


def test_controller_runs_real_cycle_and_preserves_invalid_risk_stop(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import pytest
    from app import main as main_module

    bot = SimpleNamespace(
        id=91,
        customer_id=7,
        trading_account_id=11,
        asset="forex",
        symbol="EUR_USD",
        exchange="oanda",
        timeframe="1h",
        days=30,
        risk_fraction=0.0,
        interval_seconds=60,
        mode="PAPER",
        status="RUNNING",
        next_run_at=None,
        last_run_at=None,
        last_decision="",
        last_stage="",
        last_model_version="",
        last_error="",
        consecutive_errors=0,
    )
    profile = SimpleNamespace(id=7, status="ACTIVE")
    account = SimpleNamespace(id=11, status="ACTIVE", equity=10000.0)
    state = SimpleNamespace(kill_switch=False, live_enabled=False)

    class FakeQueryResult:
        def scalars(self):
            return self

        def all(self):
            return [bot]

        def scalar_one_or_none(self):
            return bot

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, query):
            return FakeQueryResult()

        async def get(self, model, ident):
            return {
                "CustomerProfile": profile,
                "TradingAccount": account,
                "AppState": state,
            }.get(model.__name__)

        async def commit(self):
            return None

    seen_requests = []

    async def capture_decision_memory(**kwargs):
        seen_requests.append(kwargs["req"])

    async def return_true(*args, **kwargs):
        return True

    async def no_op(*args, **kwargs):
        return None

    async def forbidden_external_work(*args, **kwargs):
        raise AssertionError("invalid persisted risk must stop before external/data work")

    async def stop_after_one_cycle(seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(main_module, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(main_module, "_record_adaptive_decision_memory", capture_decision_memory)
    monkeypatch.setattr(main_module, "acquire_lock", return_true)
    monkeypatch.setattr(main_module, "refresh_lock", return_true)
    monkeypatch.setattr(main_module, "release_lock", no_op)
    monkeypatch.setattr(main_module, "_mark_worker_loop_success", lambda *args: None)
    monkeypatch.setattr(main_module.asyncio, "sleep", stop_after_one_cycle)
    monkeypatch.setattr(main_module, "_fetch_customer_oanda_data", forbidden_external_work)
    monkeypatch.setattr(main_module, "_ensure_adaptive_model_locked", forbidden_external_work)
    monkeypatch.setattr(main_module.settings, "paper_trading", True)
    monkeypatch.setattr(main_module.settings, "live_trading_enabled", False)

    async def run_controller():
        with pytest.raises(asyncio.CancelledError):
            await main_module._adaptive_bot_controller_loop()

    import anyio
    anyio.run(run_controller)

    # The scheduler builds a valid request without forwarding the corrupt stored zero;
    # the real cycle reloads risk_fraction=0.0 from persistence and fails closed.
    assert len(seen_requests) == 1
    assert 0.0 < float(seen_requests[0].risk_fraction) <= 0.02
    assert bot.status == "STOPPED"
    assert bot.next_run_at is None
    assert bot.last_error == "Persisted risk fraction is invalid; expected a finite value in (0, 0.02]"
    assert bot.last_decision == "NO_TRADE"
    assert bot.last_stage == "risk_fraction_invalid"
