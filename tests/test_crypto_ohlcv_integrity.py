from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

from app.data import _deduplicate_ohlcv_timestamps, _validate_ohlcv, fetch_crypto


def test_exact_duplicate_ohlcv_rows_collapse_to_one_candle():
    ts = pd.Timestamp("2026-01-01T00:00:00Z")
    frame = pd.DataFrame(
        [
            {"ts": ts, "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 12.0},
            {"ts": ts, "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 12.0},
        ]
    )

    result = _deduplicate_ohlcv_timestamps(frame)

    assert len(result) == 1
    assert result.iloc[0]["close"] == 101.0
    assert result.iloc[0]["volume"] == 12.0


def test_crypto_adapter_rejects_conflicting_duplicate_candle(monkeypatch):
    now_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    candle_ms = now_ms - 3_600_000 * 3

    class FakeExchange:
        def __init__(self, *_args, **_kwargs):
            self.markets = {}

        def load_markets(self):
            self.markets = {"BTC/USDT": {"spot": True, "contract": False}}
            return self.markets

        def milliseconds(self):
            return now_ms

        def parse_timeframe(self, timeframe):
            assert timeframe == "1h"
            return 3600

        def fetch_ohlcv(self, _symbol, _timeframe, *, since, limit):
            assert limit == 1000
            assert since < candle_ms
            # A pagination/provider defect: same open timestamp, different OHLCV values.
            return [
                [candle_ms, 100.0, 102.0, 99.0, 101.0, 10.0],
                [candle_ms, 100.0, 109.0, 99.0, 108.0, 25.0],
            ]

        def close(self):
            return None

    ccxt_stub = types.ModuleType("ccxt")
    ccxt_stub.exchanges = ["fake"]
    ccxt_stub.fake = FakeExchange
    monkeypatch.setitem(sys.modules, "ccxt", ccxt_stub)

    with pytest.raises(RuntimeError, match="Conflicting crypto OHLCV rows share timestamp"):
        fetch_crypto("BTC/USDT", exchange="fake", timeframe="1h", days=1)

def test_shared_ohlcv_validator_rejects_conflicting_duplicate_timestamps():
    ts = pd.Timestamp("2026-01-01T00:00:00Z")
    frame = pd.DataFrame(
        {
            "open": [100.0, 100.0, 101.0],
            "high": [102.0, 109.0, 103.0],
            "low": [99.0, 99.0, 100.0],
            "close": [101.0, 108.0, 102.0],
            "volume": [12.0, 25.0, 14.0],
        },
        index=pd.DatetimeIndex([ts, ts, ts + pd.Timedelta(hours=1)]),
    )

    with pytest.raises(RuntimeError, match="Conflicting OHLCV rows share timestamp"):
        _validate_ohlcv(frame, "1h", reject_gaps=False)


def test_shared_ohlcv_validator_rejects_invalid_timestamps():
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [102.0, 103.0, 104.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "volume": [12.0, 14.0, 15.0],
        },
        index=["2026-01-01T00:00:00Z", "not-a-timestamp", "2026-01-01T02:00:00Z"],
    )

    with pytest.raises(RuntimeError, match="invalid timestamps"):
        _validate_ohlcv(frame, "1h", reject_gaps=False)

