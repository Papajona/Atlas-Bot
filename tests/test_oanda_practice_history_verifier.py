from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from scripts.verify_oanda_practice_history import _validated_history_timestamps


def _frame(first: datetime, last: datetime) -> pd.DataFrame:
    return pd.DataFrame(index=pd.DatetimeIndex([first, last]))


def test_oanda_history_accepts_full_window_with_weekend_start_margin():
    finished = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
    started = finished - timedelta(days=365)
    frame = _frame(started + timedelta(days=2), finished - timedelta(hours=1))

    timestamps = _validated_history_timestamps(
        frame, started=started, finished=finished, days=365
    )

    assert timestamps.is_monotonic_increasing


def test_oanda_history_rejects_short_response_even_when_all_candles_are_in_window():
    finished = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
    started = finished - timedelta(days=365)
    frame = _frame(finished - timedelta(days=30), finished - timedelta(hours=1))

    with pytest.raises(SystemExit, match="does not reach the requested start"):
        _validated_history_timestamps(
            frame, started=started, finished=finished, days=365
        )


def test_oanda_history_rejects_stale_latest_candle():
    finished = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
    started = finished - timedelta(days=365)
    frame = _frame(started + timedelta(days=1), finished - timedelta(days=4))

    with pytest.raises(SystemExit, match="latest completed OANDA practice candle is stale"):
        _validated_history_timestamps(
            frame, started=started, finished=finished, days=365
        )


def test_oanda_history_rejects_naive_and_unordered_timestamps():
    finished = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
    started = finished - timedelta(days=365)
    naive = pd.DataFrame(index=pd.DatetimeIndex([started + timedelta(days=1)]))
    unordered = _frame(finished - timedelta(hours=2), started + timedelta(days=1))

    with pytest.raises(SystemExit, match="not timezone-aware"):
        _validated_history_timestamps(
            naive, started=started, finished=finished, days=365
        )
    with pytest.raises(SystemExit, match="unordered or duplicated"):
        _validated_history_timestamps(
            unordered, started=started, finished=finished, days=365
        )
