from __future__ import annotations
from datetime import time
from zoneinfo import ZoneInfo
import math
from collections.abc import Mapping
import pandas as pd

SESSION_COST_MODEL_VERSION = "market-sessions-v1"
SESSION_COST_KEYS = frozenset({"LONDON", "NEW_YORK", "NEW_YORK_LATE", "OTHER"})


def normalize_session_costs_bps(schedule: Mapping[str, object] | None) -> dict[str, float]:
    """Validate a complete per-side transaction-cost schedule for all modeled sessions."""
    if not isinstance(schedule, Mapping) or set(schedule.keys()) != SESSION_COST_KEYS:
        raise ValueError("session cost schedule must define exactly LONDON, NEW_YORK, NEW_YORK_LATE, OTHER")
    normalized: dict[str, float] = {}
    for name in sorted(SESSION_COST_KEYS):
        try:
            value = float(schedule[name])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"session cost for {name} must be finite and non-negative") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"session cost for {name} must be finite and non-negative")
        normalized[name] = value
    return normalized


def session_cost_series(index: pd.DatetimeIndex, schedule: Mapping[str, object]) -> pd.Series:
    """Map verified per-session per-side costs (bps per unit turnover) onto UTC bars."""
    costs = normalize_session_costs_bps(schedule)
    original_index = pd.DatetimeIndex(index)
    state = session_state(original_index)
    values = [costs[str(session)] for session in state["session"].tolist()]
    # session_state converts naive UTC inputs to aware timestamps for classification.
    # Return the cost vector on the original index so all aligned research streams match.
    return pd.Series(values, index=original_index, dtype="float64")

LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")


def _in_window(local_time: time, start_h: int, start_m: int, end_h: int, end_m: int) -> bool:
    x = local_time.hour * 60 + local_time.minute
    return start_h * 60 + start_m <= x < end_h * 60 + end_m


def session_label(ts: pd.Timestamp) -> str:
    ts = ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")
    london = ts.tz_convert(LONDON)
    ny = ts.tz_convert(NEW_YORK)
    if _in_window(london.time(), 8, 0, 13, 0):
        return "LONDON"
    if _in_window(ny.time(), 8, 0, 13, 0):
        return "NEW_YORK"
    if _in_window(ny.time(), 13, 0, 16, 0):
        return "NEW_YORK_LATE"
    return "OTHER"


def session_state(index: pd.DatetimeIndex) -> pd.DataFrame:
    idx = pd.DatetimeIndex(index)
    idx = idx.tz_convert("UTC") if idx.tz is not None else idx.tz_localize("UTC")
    london = idx.tz_convert(LONDON)
    ny = idx.tz_convert(NEW_YORK)
    out = pd.DataFrame(index=idx)
    out["session"] = [session_label(x) for x in idx]
    out["london_open_window"] = [_in_window(x.time(), 8, 0, 10, 0) for x in london]
    out["new_york_open_window"] = [_in_window(x.time(), 9, 30, 11, 30) for x in ny]
    out["london_local_date"] = london.date
    out["new_york_local_date"] = ny.date
    return out
