from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import numpy as np
import pandas as pd


def _frame_for_server_context(now_ms: int, *, source: str = "ccxt_ohlcv") -> pd.DataFrame:
    last_bar_ms = now_ms - 60 * 60 * 1000
    index = pd.to_datetime(
        [last_bar_ms - (39 - i) * 60 * 60 * 1000 for i in range(40)],
        unit="ms",
        utc=True,
    )
    close = 100.0 * np.cumprod(1.0 + np.linspace(-0.0005, 0.0007, len(index)))
    frame = pd.DataFrame({
        "open": close,
        "high": close * 1.001,
        "low": close * 0.999,
        "close": close,
        "volume": np.full(len(index), 100.0),
    }, index=index)
    frame.attrs["data_provenance"] = {
        "source": source,
        "symbol": "BTC/USDT",
        "exchange": "fakeexchange",
        "timeframe": "1h",
        "sha256": "a" * 64,
    }
    return frame


def _install_fake_ccxt(monkeypatch, *, spot=True, active=True, ticker_timestamp=None):
    now_ms = 2_000_000_000_000
    captured = {}

    class FakeExchange:
        markets = {}

        def __init__(self, config=None):
            captured["config"] = config

        def load_markets(self):
            return {
                "BTC/USDT": {
                    "spot": spot,
                    "contract": not spot,
                    "active": active,
                    "id": "BTCUSDT",
                    "base": "BTC",
                    "quote": "USDT",
                }
            }

        def parse_timeframe(self, timeframe):
            assert timeframe == "1h"
            return 3600

        def milliseconds(self):
            return now_ms

        def fetch_ticker(self, symbol):
            assert symbol == "BTC/USDT"
            return {
                "timestamp": now_ms if ticker_timestamp is None else ticker_timestamp,
                "bid": 99.0,
                "ask": 101.0,
            }

        def close(self):
            captured["closed"] = True

    module = types.ModuleType("ccxt")
    module.exchanges = ["fakeexchange"]
    module.fakeexchange = FakeExchange
    monkeypatch.setitem(sys.modules, "ccxt", module)
    return now_ms, captured


def test_server_paper_context_uses_fresh_closed_spot_data_and_normalized_base_volume(monkeypatch):
    from app.main import _server_paper_market_context

    now_ms, captured = _install_fake_ccxt(monkeypatch)
    frame = _frame_for_server_context(now_ms)
    req = SimpleNamespace(asset="crypto", symbol="BTC/USDT", exchange="fakeexchange", timeframe="1h")

    context = _server_paper_market_context(req, frame)

    assert context is not None
    assert context["context_source"] == "server_ccxt_closed_spot_ohlcv_and_ticker"
    assert context["market_type"] == "spot"
    assert context["volume_units_source"] == "CCXT_UNIFIED_SPOT_BASE_VOLUME"
    assert context["tradable_volume_units"] == 100.0
    assert context["bid"] == 99.0
    assert context["ask"] == 101.0
    assert context["reference_price"] == 100.0
    assert context["spread_bps"] == 200.0
    assert context["recent_volatility_bps"] >= 0
    assert context["ohlcv_sha256"] == "a" * 64
    assert captured["closed"] is True


def test_server_paper_context_fails_closed_for_derivative_volume_units(monkeypatch):
    from app.main import _server_paper_market_context

    now_ms, _ = _install_fake_ccxt(monkeypatch, spot=False)
    frame = _frame_for_server_context(now_ms)
    req = SimpleNamespace(asset="crypto", symbol="BTC/USDT", exchange="fakeexchange", timeframe="1h")

    assert _server_paper_market_context(req, frame) is None


def test_server_paper_context_fails_closed_for_inactive_market(monkeypatch):
    from app.main import _server_paper_market_context

    now_ms, _ = _install_fake_ccxt(monkeypatch, active=False)
    frame = _frame_for_server_context(now_ms)
    req = SimpleNamespace(asset="crypto", symbol="BTC/USDT", exchange="fakeexchange", timeframe="1h")

    assert _server_paper_market_context(req, frame) is None


def test_server_paper_context_fails_closed_for_stale_ticker(monkeypatch):
    from app.main import _server_paper_market_context

    now_ms, _ = _install_fake_ccxt(monkeypatch, ticker_timestamp=now_ms_minus_fifteen_seconds())
    frame = _frame_for_server_context(now_ms)
    req = SimpleNamespace(asset="crypto", symbol="BTC/USDT", exchange="fakeexchange", timeframe="1h")

    assert _server_paper_market_context(req, frame) is None


def now_ms_minus_fifteen_seconds():
    return 2_000_000_000_000 - 15_000


def test_server_paper_context_fails_closed_when_provenance_does_not_match(monkeypatch):
    from app.main import _server_paper_market_context

    now_ms, _ = _install_fake_ccxt(monkeypatch)
    frame = _frame_for_server_context(now_ms, source="yfinance")
    req = SimpleNamespace(asset="crypto", symbol="BTC/USDT", exchange="fakeexchange", timeframe="1h")

    assert _server_paper_market_context(req, frame) is None
