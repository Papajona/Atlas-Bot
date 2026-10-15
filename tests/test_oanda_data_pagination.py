from datetime import datetime, timezone, timedelta

import pandas as pd

import app.data as data


def test_oanda_fetches_entire_historical_window_across_5000_candle_pages(monkeypatch):
    calls = []
    start_time = pd.Timestamp(datetime.now(timezone.utc)) - pd.Timedelta(days=220)
    expected_count = 220 * 24

    class FakeOandaBroker:
        def __init__(self, *args, **kwargs):
            pass

        def candles(self, instrument, granularity, count, *, from_time=None, to_time=None):
            calls.append({
                "instrument": instrument,
                "granularity": granularity,
                "count": count,
                "from_time": pd.Timestamp(from_time),
                "to_time": pd.Timestamp(to_time),
            })
            assert count == 5000
            start = pd.Timestamp(from_time)
            page_index = len(calls) - 1
            size = 5000 if page_index == 0 else expected_count - 5000
            return [
                {
                    "time": (start + pd.Timedelta(hours=i)).isoformat(),
                    "complete": True,
                    "mid": {"o": "100", "h": "101", "l": "99", "c": "100"},
                    "volume": 10,
                }
                for i in range(size)
            ]

        def close(self):
            return None

    monkeypatch.setattr(data, "_attach_data_provenance", lambda frame, **kwargs: frame)
    from app.config import settings
    monkeypatch.setattr(settings, "oanda_practice", True)
    import app.forex_oanda as forex_oanda
    monkeypatch.setattr(forex_oanda, "OandaBroker", FakeOandaBroker)

    frame = data.fetch_forex_oanda("EUR_USD", timeframe="1h", days=220)

    assert len(calls) == 2
    assert len(frame) == expected_count
    assert calls[0]["from_time"] < calls[1]["from_time"]
    assert calls[0]["from_time"] == calls[1]["from_time"] - pd.Timedelta(hours=5000)
    assert frame.index.is_monotonic_increasing
    assert not frame.index.has_duplicates


def test_oanda_rejects_nonpositive_historical_window(monkeypatch):
    import app.forex_oanda as forex_oanda

    class NeverConnectBroker:
        def __init__(self, *args, **kwargs):
            raise AssertionError("invalid window must be rejected before provider access")

    monkeypatch.setattr(forex_oanda, "OandaBroker", NeverConnectBroker)
    import pytest
    with pytest.raises(ValueError, match="positive integer"):
        data.fetch_forex_oanda("EUR_USD", timeframe="1h", days=0)
