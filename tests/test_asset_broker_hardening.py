from pathlib import Path

from app.forex_oanda import OandaBroker, OandaConfig


def test_oanda_price_and_units_are_normalized_to_account_instrument_rules(monkeypatch):
    broker = OandaBroker(OandaConfig("acct", "token", True))
    monkeypatch.setattr(broker, "market_info", lambda instrument: {
        "tradeUnitsPrecision": 0,
        "displayPrecision": 3,
        "minimumTradeSize": 1,
        "maximumOrderUnits": 10000,
    })
    assert broker.validate_order_units("XAU_USD", 12.7) == 13
    assert broker.normalize_price("XAU_USD", 123.45678) == 123.457


def test_oanda_is_demo_only_and_live_customer_path_is_blocked():
    execution = Path("app/execution.py").read_text(encoding="utf-8")
    customer = Path("app/customer_oanda.py").read_text(encoding="utf-8")
    main = Path("app/main.py").read_text(encoding="utf-8")
    assert "forex_live = False" in execution
    assert "OANDA is demo/backtesting-only in AtlasRisk" in execution
    assert "OANDA live execution is disabled" in execution
    assert 'if not bool(getattr(account, "practice", True))' in customer
    assert "if settings.forex_live_enabled:" in main
    assert "live OANDA accounts are not supported" in main


def test_major_asset_universe_and_customer_connection_exist():
    cfg = Path("app/config.py").read_text(encoding="utf-8")
    main = Path("app/main.py").read_text(encoding="utf-8")
    assert "EUR_USD,GBP_USD,USD_JPY,USD_CHF,AUD_USD,USD_CAD,NZD_USD" in cfg
    assert "XAU_USD,XAG_USD,WTICO_USD,BCO_USD,NATGAS_USD" in cfg
    assert "/api/customer/broker/oanda/connect" in main
    assert "CustomerOandaAccount" in main


def test_customer_ui_exposes_fx_and_commodity_markets():
    html = Path("app/templates/customer.html").read_text(encoding="utf-8")
    assert 'value="forex">Major FX' in html
    assert 'value="commodity">Commodities' in html
    assert "XAU_USD" in html
    assert "EUR_USD" in html


# OANDA practice execution has its own fail-closed gate; it must not be routed through
# the real-money live gate (which intentionally rejects forex/commodities).
import asyncio
from types import SimpleNamespace

import pytest

from app.config import settings
from app.live_execution import LiveExecutionBlocked, assert_live_system_enabled


class _PracticeGateResult:
    def __init__(self, state):
        self.state = state

    def scalar_one_or_none(self):
        return self.state


class _PracticeGateDB:
    def __init__(self, state):
        self.state = state

    async def execute(self, _statement):
        return _PracticeGateResult(self.state)


def _practice_gate_db(*, kill_switch=False, enabled=True):
    return _PracticeGateDB(SimpleNamespace(
        id=1, kill_switch=kill_switch, live_enabled=False, forex_demo_enabled=enabled,
    ))


def _run_practice_gate(db, **overrides):
    args = dict(asset="forex", customer_id=None, exchange="oanda", side="buy", quantity=1.0, practice_demo=True)
    args.update(overrides)
    return asyncio.run(assert_live_system_enabled(db, **args))


def test_oanda_practice_gate_allows_demo_when_real_money_live_mode_is_off(monkeypatch):
    monkeypatch.setattr(settings, "oanda_practice", True)
    monkeypatch.setattr(settings, "forex_live_enabled", False)
    monkeypatch.setattr(settings, "oanda_account_id", "practice-account")
    monkeypatch.setattr(settings, "oanda_api_token", "practice-token")
    # These settings must not grant real-money authority, but also must not block the practice endpoint.
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "paper_trading", True)
    monkeypatch.setattr(settings, "broker_sandbox", True)
    assert _run_practice_gate(_practice_gate_db()) is None


def test_oanda_practice_gate_fails_closed_for_customer_or_kill_switch(monkeypatch):
    monkeypatch.setattr(settings, "oanda_practice", True)
    monkeypatch.setattr(settings, "forex_live_enabled", False)
    monkeypatch.setattr(settings, "oanda_account_id", "practice-account")
    monkeypatch.setattr(settings, "oanda_api_token", "practice-token")
    with pytest.raises(LiveExecutionBlocked, match="Customer OANDA execution"):
        _run_practice_gate(_practice_gate_db(), customer_id=123)
    with pytest.raises(LiveExecutionBlocked, match="kill switch"):
        _run_practice_gate(_practice_gate_db(kill_switch=True))


def test_oanda_practice_gate_requires_explicit_practice_state_and_credentials(monkeypatch):
    monkeypatch.setattr(settings, "oanda_practice", True)
    monkeypatch.setattr(settings, "forex_live_enabled", False)
    monkeypatch.setattr(settings, "oanda_account_id", "")
    monkeypatch.setattr(settings, "oanda_api_token", "")
    with pytest.raises(LiveExecutionBlocked, match="not configured"):
        _run_practice_gate(_practice_gate_db())
    monkeypatch.setattr(settings, "oanda_account_id", "practice-account")
    monkeypatch.setattr(settings, "oanda_api_token", "practice-token")
    with pytest.raises(LiveExecutionBlocked, match="not enabled"):
        _run_practice_gate(_practice_gate_db(enabled=False))
    with pytest.raises(LiveExecutionBlocked, match="demo-only"):
        _run_practice_gate(_practice_gate_db(), practice_demo=False)
    monkeypatch.setattr(settings, "forex_live_enabled", True)
    with pytest.raises(LiveExecutionBlocked, match="live OANDA must remain disabled"):
        _run_practice_gate(_practice_gate_db())
