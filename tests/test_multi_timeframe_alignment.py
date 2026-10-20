from __future__ import annotations

import pandas as pd

from app.multi_timeframe import _map_back


def test_higher_timeframe_value_is_available_on_next_base_bar_not_next_htf_bar():
    index = pd.date_range("2026-01-01T00:00:00Z", periods=13, freq="5min")
    completed = pd.Series([1.0, 2.0, 3.0], index=[index[4], index[8], index[12]])

    mapped = _map_back(completed, index)

    # The value stamped at 00:20 represents a completed HTF bar, but a source bar
    # opening at 00:20 cannot consume it until the next 5-minute base-bar open.
    assert pd.isna(mapped.loc[index[4]])
    assert mapped.loc[index[5]] == 1.0
    assert mapped.loc[index[8]] == 1.0
    assert mapped.loc[index[9]] == 2.0
    # A value whose availability boundary lies outside the input window is not leaked.
    assert mapped.loc[index[12]] == 2.0
