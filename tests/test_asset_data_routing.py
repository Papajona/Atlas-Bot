from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("ENVIRONMENT", "test")


def test_yahoo_fx_symbol_normalization_only_accepts_currency_pairs():
    from app.data import _normalize_yfinance_fx_symbol

    assert _normalize_yfinance_fx_symbol("EUR_USD") == "EURUSD=X"
    assert _normalize_yfinance_fx_symbol("EUR/USD") == "EURUSD=X"
    assert _normalize_yfinance_fx_symbol("EURUSD=X") == "EURUSD=X"
    assert _normalize_yfinance_fx_symbol("gbpusd") == "GBPUSD=X"

    for symbol in ("XAU_USD", "GC=F", "CL=F", "SPY", "", "EUR_USD=X"):
        with pytest.raises(ValueError, match="six-letter currency-pair"):
            _normalize_yfinance_fx_symbol(symbol)


def test_yahoo_fx_adapter_rejects_unsupported_timeframe_instead_of_substituting_hourly():
    from app.data import fetch_forex

    with pytest.raises(ValueError, match="Unsupported Yahoo Finance FX timeframe '4h'"):
        fetch_forex("EUR_USD", timeframe="4h", days=30)


def test_market_data_never_routes_commodity_symbols_through_yahoo_fx(monkeypatch):
    from app import main

    monkeypatch.setattr(main.settings, "forex_broker", "oanda")
    monkeypatch.setattr(main.settings, "oanda_account_id", "")
    monkeypatch.setattr(main.settings, "oanda_api_token", "")
    monkeypatch.setattr(
        main,
        "fetch_forex",
        lambda *args, **kwargs: pytest.fail("commodity symbols must never enter the Yahoo FX adapter"),
    )

    request = SimpleNamespace(
        asset="commodity", symbol="XAU_USD", exchange="oanda",
        timeframe="1h", days=30,
    )
    with pytest.raises(RuntimeError, match="Commodity market data requires configured OANDA practice credentials"):
        main.market_data(request)


def test_market_data_uses_oanda_practice_for_configured_commodity_instrument(monkeypatch):
    from app import main

    monkeypatch.setattr(main.settings, "forex_broker", "oanda")
    monkeypatch.setattr(main.settings, "oanda_account_id", "practice-account")
    monkeypatch.setattr(main.settings, "oanda_api_token", "practice-token")
    calls = []
    monkeypatch.setattr(
        main, "fetch_forex_oanda",
        lambda symbol, timeframe, days: calls.append((symbol, timeframe, days)) or {"source": "oanda"},
    )
    request = SimpleNamespace(
        asset="commodity", symbol="XAUUSD", exchange="oanda",
        timeframe="4h", days=30,
    )

    assert main.market_data(request) == {"source": "oanda"}
    assert calls == [("XAU_USD", "4h", 30)]


def test_market_data_rejects_unconfigured_asset_classes(monkeypatch):
    from app import main

    request = SimpleNamespace(
        asset="stocks", symbol="SPY", exchange="yfinance",
        timeframe="1h", days=30,
    )
    with pytest.raises(RuntimeError, match="Unsupported market-data asset class"):
        main.market_data(request)
