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


def test_adaptive_controller_defers_persisted_risk_validation_to_cycle():
    src = (ROOT / "app" / "main.py").read_text()
    start = src.index("async def _adaptive_bot_controller_loop")
    end = src.index("async def _fetch_customer_oanda_data", start)
    chunk = src[start:end]
    assert "days, _risk_fraction, interval_seconds, mode in work" in chunk
    assert "risk_fraction=risk_fraction" not in chunk
    assert "risk_fraction_invalid/NO_TRADE" in chunk


def test_adaptive_controller_preserves_stopped_validation_state(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import pytest
    from app import main as main_module

    bot = SimpleNamespace(
        id=91,
        customer_id=7,
        trading_account_id=11,
        asset="crypto",
        symbol="BTC/USDT",
        exchange="binance",
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

    class FakeQueryResult:
        def scalars(self):
            return self

        def all(self):
            return [bot]

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, query):
            return FakeQueryResult()

        async def get(self, model, bot_id):
            return bot if bot_id == bot.id else None

        async def commit(self):
            return None

    async def fake_cycle(bot_id, req, *, decision_id=None):
        # Simulate the cycle's fail-closed persisted-risk validation result.
        bot.status = "STOPPED"
        bot.next_run_at = None
        bot.last_error = "Persisted risk fraction is invalid; expected a finite value in (0, 0.02]"
        bot.last_decision = "NO_TRADE"
        bot.last_stage = "risk_fraction_invalid"
        return {
            "decision": "NO_TRADE",
            "stage": "risk_fraction_invalid",
            "reason": "Persisted risk fraction must be finite and in (0, 0.02]",
        }

    async def no_op(*args, **kwargs):
        return None

    async def stop_after_one_cycle(seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(main_module, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(main_module, "_run_persisted_adaptive_bot_cycle", fake_cycle)
    monkeypatch.setattr(main_module, "_record_adaptive_decision_memory", no_op)
    monkeypatch.setattr(main_module, "acquire_lock", no_op)
    monkeypatch.setattr(main_module, "refresh_lock", lambda *args, **kwargs: _async_true())
    monkeypatch.setattr(main_module, "release_lock", no_op)
    monkeypatch.setattr(main_module, "_mark_worker_loop_success", lambda *args: None)
    monkeypatch.setattr(main_module.asyncio, "sleep", stop_after_one_cycle)

    async def run_controller():
        with pytest.raises(asyncio.CancelledError):
            await main_module._adaptive_bot_controller_loop()

    async def _async_true():
        return True

    import anyio
    anyio.run(run_controller)

    assert bot.status == "STOPPED"
    assert bot.next_run_at is None
    assert bot.last_error == "Persisted risk fraction is invalid; expected a finite value in (0, 0.02]"
    assert bot.last_decision == "NO_TRADE"
    assert bot.last_stage == "risk_fraction_invalid"
