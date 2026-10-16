import pandas as pd
import pytest

import app.data as data


def test_oanda_pages_respect_the_documented_count_from_to_contract(monkeypatch):
    """OANDA: count must not be given with both from and to, and a from-to range may not exceed 5000 candles."""
    calls = []
    total = 365 * 24  # one year of H1 bars, well above one 5000-candle page

    class StrictOandaBroker:
        def __init__(self, *args, **kwargs):
            pass

        def candles(self, instrument, granularity, count, *, from_time=None, to_time=None):
            calls.append((from_time, to_time))
            if from_time and to_time:
                raise RuntimeError("count cannot be combined with both from and to / range exceeds 5000 candles")
            start = pd.Timestamp(from_time)
            done = (len(calls) - 1) * 5000
            size = min(5000, total - done)
            return [
                {"time": (start + pd.Timedelta(hours=i)).isoformat(), "complete": True,
                 "mid": {"o": "100", "h": "101", "l": "99", "c": "100"}, "volume": 1}
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


def test_oanda_results_are_clipped_to_requested_utc_window(monkeypatch):
    fixed_now = pd.Timestamp("2026-01-03T00:30:00Z").to_pydatetime()

    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            return fixed_now.astimezone(tz) if tz else fixed_now

    class OutOfWindowBroker:
        def __init__(self, *args, **kwargs):
            pass

        def candles(self, instrument, granularity, count, *, from_time=None, to_time=None):
            return [
                {"time": stamp, "complete": True,
                 "mid": {"o": "100", "h": "101", "l": "99", "c": "100"}, "volume": 1}
                for stamp in (
                    "2026-01-01T00:00:00Z",  # before requested start (00:30)
                    "2026-01-01T01:00:00Z",
                    "2026-01-03T00:00:00Z",
                    "2026-01-03T01:00:00Z",  # at/after requested end (00:30)
                )
            ]

        def close(self):
            return None

    monkeypatch.setattr(data, "datetime", FixedDateTime)
    monkeypatch.setattr(data, "_attach_data_provenance", lambda frame, **kwargs: frame)
    from app.config import settings
    monkeypatch.setattr(settings, "oanda_practice", True)
    import app.forex_oanda as forex_oanda
    monkeypatch.setattr(forex_oanda, "OandaBroker", OutOfWindowBroker)

    frame = data.fetch_forex_oanda("EUR_USD", timeframe="1h", days=2)
    assert frame.index.min() >= pd.Timestamp("2026-01-01T00:30:00Z")
    assert frame.index.max() < pd.Timestamp("2026-01-03T00:30:00Z")
    assert frame.index.tolist() == [
        pd.Timestamp("2026-01-01T01:00:00Z"),
        pd.Timestamp("2026-01-03T00:00:00Z"),
    ]
