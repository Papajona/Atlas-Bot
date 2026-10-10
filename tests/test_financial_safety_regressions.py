from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_customer_forex_bot_cycle_stops_before_data_or_ai_without_simulation_ledger(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import app.main as main
    from app.config import settings

    bot = SimpleNamespace(
        id=1, status="RUNNING", customer_id=7, trading_account_id=3,
        mode="PAPER", risk_fraction=0.01, next_run_at=None, last_error="",
    )
    profile = SimpleNamespace(id=7, status="ACTIVE")
    account = SimpleNamespace(id=3, status="ACTIVE", equity=1000.0)
    state = SimpleNamespace(kill_switch=False, live_enabled=True)

    class Result:
        def scalar_one_or_none(self):
            return bot

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, *args, **kwargs):
            return Result()

        async def get(self, model, key):
            return {
                main.CustomerProfile: profile,
                main.TradingAccount: account,
                main.AppState: state,
            }.get(model)

        async def commit(self):
            return None

    monkeypatch.setattr(main, "SessionLocal", lambda: FakeDB())
    monkeypatch.setattr(settings, "paper_trading", True)
    calls = {"market_data": 0, "ai": 0}

    async def unexpected_market_data(*args, **kwargs):
        calls["market_data"] += 1
        raise AssertionError("market data must not be fetched before the paper-ledger gate")

    async def unexpected_ai(*args, **kwargs):
        calls["ai"] += 1
        raise AssertionError("AI must not be called before the paper-ledger gate")

    monkeypatch.setattr(main, "_fetch_customer_oanda_data", unexpected_market_data)
    monkeypatch.setattr(main, "_ensure_adaptive_model_locked", unexpected_ai)

    result = asyncio.run(main._run_persisted_adaptive_bot_cycle(
        1,
        SimpleNamespace(asset="forex", symbol="EUR/USD", exchange="oanda", timeframe="1h"),
    ))

    assert result["decision"] == "NO_TRADE"
    assert result["stage"] == "paper_simulation_ledger_gate"
    assert bot.status == "STOPPED"
    assert calls == {"market_data": 0, "ai": 0}


def test_live_customer_equity_is_recomputed_from_ledger_and_open_positions():
    source = _source("app/execution.py")
    assert "async def _refresh_live_customer_equity" in source
    assert 'balance["available"] + balance["trading_reserved"]' in source
    assert "account.equity = max(0.0, account.cash_equity + unrealized)" in source


def test_rbac_inactive_assignment_blocks_allowlist_fallback():
    source = _source("app/admin_rbac.py")
    assert "select(AdminRole.role, AdminRole.active)" in source
    assert "if rows:" in source
    assert "if uid in allowed:" in source


def test_oos_gate_missing_required_metrics_fails_closed():
    source = _source("app/research_validation.py")
    assert 'sharpe is not None and sharpe >= float(min_sharpe)' in source
    assert 'max_dd is not None and max_dd >= float(max_drawdown)' in source
    assert 'trades is not None and trades >= int(min_trades)' in source
    assert '("oos_total_return" in oos or "total_return" in oos)' in source


def test_walk_forward_fold_returns_are_shifted():
    source = _source("app/trading_core.py")
    assert "fold_position = np.r_[0, sig[:-1]]" in source
    assert "fold_net = fold_position * fold_ret" in source


def test_customer_live_venue_is_locked_to_binance_spot():
    config = _source("app/config.py")
    execution = _source("app/customer_binance_execution.py")
    assert 'customer_live_exchange: str = "binance"' in config
    assert 'customer_live_market_type: str = "spot"' in config
    assert 'Customer live venue is not configured for Binance' in execution
    assert 'Customer live market type must be spot' in execution


def test_funding_webhook_requires_fresh_timestamp_bound_signature():
    source = _source("app/main.py")
    assert "x_funding_timestamp" in source
    assert "funding_webhook_max_skew_seconds" in _source("app/config.py")
    assert 'signed = str(timestamp).strip().encode() + b"." + raw_body' in source


def test_supervisor_restarts_worker_when_audit_raises(monkeypatch):
    import asyncio

    import app.main as main

    async def failing_audit(*args, **kwargs):
        raise RuntimeError("simulated database outage during audit")

    monkeypatch.setattr(main, "_audit", failing_audit)

    async def run():
        attempts = 0
        restarted = asyncio.Event()

        async def worker():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await main._audit("WORKER_FAILURE", {"test": True})
            restarted.set()
            await asyncio.Future()

        task = asyncio.create_task(main._supervise_worker(
            worker, "audit-failure-test", restart_delay=0.001, max_restart_delay=0.005
        ))
        try:
            await asyncio.wait_for(restarted.wait(), timeout=0.25)
            assert attempts == 2
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(run())
