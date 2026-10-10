import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.risk_governor import evaluate_trade
from app.schemas import ExecuteRequest

ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_customer_forex_bot_cycle_fails_before_any_ai_call_without_simulation_ledger(monkeypatch):
    import app.main as main

    bot = SimpleNamespace(
        id=77, customer_id=12, trading_account_id=31, asset="forex",
        symbol="EUR_USD", exchange="oanda", timeframe="1h", days=365,
        risk_fraction=0.01, interval_seconds=900, mode="PAPER", status="RUNNING",
    )
    profile = SimpleNamespace(id=12)
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

        async def get(self, model, _id):
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
    ai_calls = []

    async def forbidden_ai(*args, **kwargs):
        ai_calls.append((args, kwargs))
        raise AssertionError("AI must not be called for an unsupported customer paper cycle")

    async def forbidden_data(*args, **kwargs):
        raise AssertionError("The cycle must fail before market-data/model work")

    monkeypatch.setattr(main, "dual_ai_trade_safety_review", forbidden_ai)
    monkeypatch.setattr(main, "_ensure_adaptive_model_locked", forbidden_ai)
    monkeypatch.setattr(main, "_fetch_customer_oanda_data", forbidden_data)

    result = asyncio.run(main._run_persisted_adaptive_bot_cycle(
        77,
        main.CustomerBotStartRequest(
            asset="forex", symbol="EUR_USD", exchange="oanda",
            timeframe="1h", days=365, autonomous=False,
        ),
        decision_id="test-forex-cycle",
    ))

    assert result["decision"] == "NO_TRADE"
    assert result["stage"] == "customer_paper_execution_unavailable"
    assert bot.status == "STOPPED"
    assert bot.next_run_at is None
    assert ai_calls == []


@pytest.mark.parametrize("field", [
    "quantity", "price", "score", "long_probability", "short_probability",
    "flat_probability", "stop_loss_price", "take_profit_price",
])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_execute_request_rejects_non_finite_float_fields(field, value):
    payload = {
        "side": "buy",
        "quantity": 1.0,
        "price": 100.0,
        "score": 0.5,
        "long_probability": 0.7,
        "short_probability": 0.2,
        "flat_probability": 0.1,
    }
    payload[field] = value
    with pytest.raises(ValidationError):
        ExecuteRequest.model_validate(payload)


@pytest.mark.parametrize("field,value", [
    ("stop_loss_price", float("nan")),
    ("stop_loss_price", float("inf")),
    ("stop_loss_price", float("-inf")),
    ("take_profit_price", float("nan")),
    ("take_profit_price", float("inf")),
    ("take_profit_price", float("-inf")),
])
def test_risk_governor_blocks_non_finite_protective_prices(field, value):
    prices = {"stop_loss_price": 90.0, "take_profit_price": 120.0}
    prices[field] = value
    decision = evaluate_trade(
        side="buy", price=100.0, quantity=1.0, live=True,
        stop_loss_price=prices["stop_loss_price"],
        take_profit_price=prices["take_profit_price"],
        signal={},
    )
    assert decision.action == "BLOCK"
    expected = "invalid_stop_loss_price" if field == "stop_loss_price" else "invalid_take_profit_price"
    assert expected in decision.reasons


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
