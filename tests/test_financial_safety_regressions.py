from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_customer_forex_cycle_fails_before_model_calls_without_simulation_ledger(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import app.main as main
    from app.config import settings

    calls = []
    bot = SimpleNamespace(
        id=41, customer_id=7, trading_account_id=9, status="RUNNING",
        asset="forex", symbol="EUR/USD", exchange="oanda", timeframe="1h",
        days=30, risk_fraction=0.01, interval_seconds=300, mode="PAPER",
    )
    profile = SimpleNamespace(id=7)
    account = SimpleNamespace(status="ACTIVE", equity=10000.0)
    state = SimpleNamespace(kill_switch=False, live_enabled=False)

    class Result:
        def scalar_one_or_none(self):
            return bot

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def execute(self, _statement):
            return Result()

        async def get(self, model, _ident):
            if model is main.CustomerProfile:
                return profile
            if model is main.TradingAccount:
                return account
            if model is main.AppState:
                return state
            raise AssertionError(f"unexpected model lookup: {model}")

    monkeypatch.setattr(main, "SessionLocal", lambda: Session())
    monkeypatch.setattr(settings, "paper_trading", True)
    monkeypatch.setattr(settings, "live_trading_enabled", False)

    async def forbidden_model_or_data_call(*_args, **_kwargs):
        calls.append("called")
        raise AssertionError("unsupported customer forex cycle must stop before data/model/AI work")

    monkeypatch.setattr(main, "_fetch_customer_oanda_data", forbidden_model_or_data_call)
    monkeypatch.setattr(main, "_ensure_adaptive_model_locked", forbidden_model_or_data_call)

    req = SimpleNamespace(asset="forex", symbol="EUR/USD", exchange="oanda", timeframe="1h", days=30)
    result = asyncio.run(main._run_persisted_adaptive_bot_cycle(41, req))

    assert result["decision"] == "NO_TRADE"
    assert result["stage"] == "simulation_ledger_gate"
    assert calls == []


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


def test_customer_forex_start_fails_before_bot_creation_or_model_calls(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import pytest
    from fastapi import HTTPException
    import app.main as main
    from app.config import settings

    calls = []
    profile = SimpleNamespace(id=7)
    state = SimpleNamespace(kill_switch=False, live_enabled=False)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, model, _ident):
            if model is main.AppState:
                return state
            raise AssertionError(f"unexpected database lookup: {model}")

        async def commit(self):
            calls.append("commit")

    async def get_customer(*_args, **_kwargs):
        return profile, {}

    async def entitlements(*_args, **_kwargs):
        return {"live": True}, None

    async def forbidden_downstream(*_args, **_kwargs):
        calls.append("downstream")
        raise AssertionError("unsupported forex bot must fail before account creation or model calls")

    monkeypatch.setattr(main, "SessionLocal", lambda: Session())
    monkeypatch.setattr(main, "get_customer", get_customer)
    monkeypatch.setattr(main, "_subscription_entitlements", entitlements)
    monkeypatch.setattr(main, "_assert_established_noncrypto", lambda _req: None)
    monkeypatch.setattr(main, "_get_or_create_customer_trading_account", forbidden_downstream)
    monkeypatch.setattr(main, "_fetch_customer_oanda_data", forbidden_downstream)
    monkeypatch.setattr(main, "dual_ai_trade_safety_review", forbidden_downstream)
    monkeypatch.setattr(settings, "strategy_engine_enabled", True)
    monkeypatch.setattr(settings, "paper_trading", True)
    monkeypatch.setattr(settings, "live_trading_enabled", False)

    req = SimpleNamespace(
        asset="forex", symbol="EUR/USD", exchange="oanda", timeframe="1h",
        days=30, risk_fraction=0.01, autonomous=True, interval_seconds=300,
        strategy_candidate_id=None,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.customer_start_bot(req, authorization="Bearer test-token"))

    assert exc.value.status_code == 409
    assert calls == []
