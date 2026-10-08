import sys
import types

import numpy as np
import pandas as pd


def _sample_ohlcv():
    idx = pd.date_range("2026-01-01", periods=5, freq="h", tz="UTC")
    close = np.array([100.0, 101.0, 100.5, 102.0, 101.5])
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(len(close), 100.0),
        },
        index=idx,
    )


def test_commodity_profile_exists():
    from app.trading_core import PROFILES

    assert "commodity" in PROFILES
    profile = PROFILES["commodity"]
    assert profile.bars_per_year > 0
    assert profile.max_hold > 0
    assert profile.taker_bps > 0
    assert profile.slippage_bps > 0


def test_commodity_yahoo_symbol_mapping():
    from app.data import _commodity_yahoo_symbol

    assert _commodity_yahoo_symbol("XAU_USD") == "GC=F"
    assert _commodity_yahoo_symbol("XAG_USD") == "SI=F"
    assert _commodity_yahoo_symbol("WTICO_USD") == "CL=F"
    assert _commodity_yahoo_symbol("BCO_USD") == "BZ=F"
    assert _commodity_yahoo_symbol("NATGAS_USD") == "NG=F"
    assert _commodity_yahoo_symbol("GC=F") == "GC=F"


def test_fetch_commodity_uses_futures_mapping_and_provenance(monkeypatch):
    from app import data

    calls = {}

    def fake_download(symbol, **kwargs):
        calls["symbol"] = symbol
        calls["kwargs"] = kwargs
        return _sample_ohlcv()

    fake_yfinance = types.SimpleNamespace(download=fake_download)
    monkeypatch.setitem(sys.modules, "yfinance", fake_yfinance)

    out = data.fetch_commodity("XAU_USD", timeframe="1h", days=30)

    assert calls["symbol"] == "GC=F"
    assert calls["kwargs"]["interval"] == "1h"
    assert out.attrs["data_provenance"]["source"] == "yfinance_commodity_futures"
    assert out.attrs["data_provenance"]["symbol"] == "XAU_USD"
    assert out.attrs["data_provenance"]["row_count"] == 5
    assert len(out.attrs["data_provenance"]["sha256"]) == 64


def test_learning_commodity_dispatches_to_commodity_adapter(monkeypatch):
    from app import trade_learning

    expected = _sample_ohlcv()
    calls = {}

    def fake_fetch(symbol, timeframe, days):
        calls.update(symbol=symbol, timeframe=timeframe, days=days)
        return expected

    monkeypatch.setattr(trade_learning, "fetch_commodity", fake_fetch)

    episode = types.SimpleNamespace(
        asset="commodity",
        symbol="XAU_USD",
        timeframe="1h",
        exchange="oanda",
    )
    out = trade_learning._fetch_learning_data(episode, days=30)

    assert out is expected
    assert calls == {"symbol": "XAU_USD", "timeframe": "1h", "days": 30}
