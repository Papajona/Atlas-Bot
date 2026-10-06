from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from app.trading_requirements import deriv_requirement, oanda_requirement, spot_requirement


def test_spot_requirement_uses_provider_minimum_and_risk_floor():
    req = spot_requirement(
        venue="binance",
        symbol="BTC/USDT",
        price=100.0,
        market={"limits": {"amount": {"min": 0.01}, "cost": {"min": 5.0}}},
        stop_loss_price=99.0,
        risk_per_trade=0.005,
        risk_tolerance=0.10,
    )
    assert req.minimum_order_notional == 5.0
    assert req.risk_required_balance == 1.8181818181818181
    assert req.minimum_balance == req.risk_required_balance


def test_spot_requirement_does_not_invent_risk_without_stop():
    req = spot_requirement(
        venue="binance",
        symbol="BTC/USDT",
        price=100.0,
        market={"limits": {"amount": {"min": 0.01}, "cost": {"min": 5.0}}},
        stop_loss_price=None,
        risk_per_trade=0.005,
        risk_tolerance=0.10,
    )
    assert req.minimum_balance == 5.0
    assert "risk_required_balance_not_calculated_without_stop" in req.reasons


def test_oanda_cross_currency_is_not_guessed():
    req = oanda_requirement(
        symbol="EUR_GBP",
        price=0.85,
        minimum_trade_size=1,
        stop_loss_price=0.84,
        risk_per_trade=0.005,
        risk_tolerance=0.10,
        account_currency="USD",
    )
    assert req.minimum_order_notional is None
    assert "quote_to_account_currency_conversion_required" in req.reasons
    assert req.risk_required_balance is not None


def test_deriv_minimum_is_provider_quoted_not_hardcoded():
    req = deriv_requirement()
    assert req.status == "PROVIDER_QUOTE_REQUIRED"
    assert req.minimum_balance is None


def test_customer_requirement_endpoint_never_enables_execution():
    source = (ROOT / "app/main.py").read_text()
    start = source.index('@app.post("/api/customer/trading-requirements")')
    end = source.index('@app.get("/api/customer/bot")', start)
    block = source[start:end]
    assert "never submits an order" in block
    assert "build_customer_binance_broker" in block
    assert "build_customer_oanda_broker" in block
    assert "deriv_requirement" in block
    assert ".market_order(" not in block
    assert ".buy(" not in block
