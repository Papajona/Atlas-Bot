import pandas as pd
import pytest

import app.data as data


def test_oanda_pages_respect_the_documented_count_from_to_contract(monkeypatch):
    """OANDA: count must not be given with both from and to, and a range may not exceed 5000 candles."""
    calls = []
    total = 365 * 24  # one year of H1 bars, well above one 5000-candle page

    class StrictOandaBroker:
        def __init__(self, *args, **kwargs):
            pass

        def candles(self, instrument, granularity, count, *, from_time=None, to_time=None):
            calls.append((from_time, to_time))
            if from_time and to_time:
                raise RuntimeError(
                    "count cannot be combined with both from and to / range exceeds 5000 candles"
                )
            start = pd.Timestamp(from_time)
            done = (len(calls) - 1) * 5000
            size = min(5000, total - done)
            return [
                {
                    "time": (start + pd.Timedelta(hours=i)).isoformat(),
                    "complete": True,
                    "mid": {"o": "100", "h": "101", "l": "99", "c": "100"},
                    "volume": 1,
                }
                for i in range(size)
            ]

        def close(self):
            return None

    monkeypatch.setattr(data, "_attach_data_provenance", lambda frame, **kwargs: frame)
    from app.config import settings
    monkeypatch.setattr(settings, "oanda_practice", True)
    import app.forex_oanda as forex_oanda
    monkeypatch.setattr(forex_oanda, "OandaBroker", StrictOandaBroker)

    frame = data.fetch_forex_oanda("EUR_USD", timeframe="1h", days=365)
    assert len(calls) == 2 and len(frame) == total
    assert all(not (start and end) for start, end in calls)
