"""Per-trade performance ratios for the risk dashboard.

Avoid the previous fixed hourly annualisation and fabricated values for zero-volatility
or zero-drawdown samples. Ratios are annualised only when observed trade timestamps
establish the sample frequency.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Iterable, Sequence

_SECONDS_PER_YEAR = 365.25 * 86400.0
MIN_ANNUALIZATION_SPAN_SECONDS = 30 * 86400
MIN_SAMPLE = 10


def _as_utc(value) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def performance_ratios(
    returns: Sequence[float],
    timestamps: Iterable | None = None,
    current_drawdown_pct: float = 0.0,
) -> dict:
    """Return Sharpe/Calmar/Sortino/edge-decay metrics without inventing inputs."""
    timestamp_values = list(timestamps) if timestamps is not None else None
    clean = []
    paired_times = []
    timestamps_complete = timestamp_values is not None
    for index, value in enumerate(returns):
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(number):
            continue
        clean.append(number)
        stamp = (
            _as_utc(timestamp_values[index])
            if timestamp_values is not None and index < len(timestamp_values)
            else None
        )
        if stamp is None:
            timestamps_complete = False
        else:
            paired_times.append(stamp)

    if timestamp_values is not None and len(timestamp_values) != len(returns):
        timestamps_complete = False

    zero = {
        "sharpe": 0.0,
        "calmar": 0.0,
        "sortino": 0.0,
        "edge_decay_z": 0.0,
        "annualized": False,
        "trades_per_year": None,
        "sample_size": len(clean),
    }
    if len(clean) < MIN_SAMPLE:
        return zero

    n = len(clean)
    mean_ret = sum(clean) / n
    vol = math.sqrt(sum((value - mean_ret) ** 2 for value in clean) / n)
    downside = [value for value in clean if value < 0]
    downside_vol = math.sqrt(sum(value * value for value in downside) / max(1, len(downside)))

    periods = 1.0
    annualized = False
    trades_per_year = None
    if timestamps_complete and len(paired_times) == n and len(paired_times) >= 2:
        times = sorted(paired_times)
        span = (times[-1] - times[0]).total_seconds()
        # Short bursts and partial-fill clusters do not establish a stable cadence.
        if span >= MIN_ANNUALIZATION_SPAN_SECONDS:
            interval_count = len(times) - 1
            trades_per_year = interval_count / (span / _SECONDS_PER_YEAR)
            if math.isfinite(trades_per_year) and trades_per_year > 0:
                periods = trades_per_year
                annualized = True

    root = math.sqrt(periods)
    try:
        drawdown = max(0.0, float(current_drawdown_pct)) / 100.0
    except (TypeError, ValueError, OverflowError):
        drawdown = 0.0
    if not math.isfinite(drawdown):
        drawdown = 0.0

    return {
        "sharpe": (mean_ret / vol * root) if vol > 0 else 0.0,
        "calmar": (mean_ret * periods / max(0.01, drawdown)) if drawdown > 0 else 0.0,
        "sortino": (mean_ret / downside_vol * root) if downside_vol > 0 else 0.0,
        "edge_decay_z": (mean_ret / (vol / math.sqrt(n))) if vol > 0 else 0.0,
        "annualized": annualized,
        "trades_per_year": trades_per_year,
        "sample_size": n,
    }
