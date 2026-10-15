"""Execution-aware research simulation with intrabar stops, targets, and carry.

This module is offline research only. It never submits orders. Ambiguous OHLC bars are
resolved conservatively (stop before target). Financing is included only when an evidenced
per-bar rate is supplied; missing carry is exposed as incomplete research evidence.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def _finite_nonnegative(value: object, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite and non-negative") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return parsed


def execution_turnover(position: pd.Series, exit_mask: pd.Series | None = None) -> pd.Series:
    """Entry/resize/reversal turnover, including the close on intrabar-exit bars."""
    positions = pd.Series(position).astype(float)
    exits = (
        pd.Series(False, index=positions.index)
        if exit_mask is None
        else pd.Series(exit_mask, index=positions.index).fillna(False).astype(bool)
    )
    out = np.zeros(len(positions), dtype=float)
    previous = 0.0
    for i, current in enumerate(positions.to_numpy(dtype=float)):
        if not math.isfinite(float(current)):
            raise ValueError("position stream contains a non-finite value")
        out[i] = abs(float(current) - previous)
        if bool(exits.iloc[i]) and current != 0.0:
            out[i] += abs(float(current))
            previous = 0.0
        else:
            previous = float(current)
    return pd.Series(out, index=positions.index, dtype=float)


def costed_execution_returns(
    position: pd.Series,
    market_returns: pd.Series,
    *,
    transaction_cost_bps: float,
    carry_bps_per_bar: float | None,
    exit_mask: pd.Series | None = None,
    cost_multiplier: float = 1.0,
) -> pd.Series:
    """Compute net returns using transaction turnover and per-bar financing costs."""
    cost_bps = _finite_nonnegative(transaction_cost_bps, "transaction_cost_bps")
    multiplier = _finite_nonnegative(cost_multiplier, "cost_multiplier")
    if multiplier <= 0:
        raise ValueError("cost_multiplier must be greater than zero")
    carry = (
        None if carry_bps_per_bar is None
        else _finite_nonnegative(carry_bps_per_bar, "carry_bps_per_bar")
    )
    pos = pd.Series(position).astype(float)
    returns = pd.Series(market_returns, index=pos.index).astype(float)
    if not np.isfinite(pos.to_numpy()).all() or not np.isfinite(returns.to_numpy()).all():
        raise ValueError("position and market-return streams must be finite")
    turnover = execution_turnover(pos, exit_mask)
    net = pos * returns - turnover * (cost_bps * multiplier / 10_000.0)
    if carry is not None:
        net = net - pos.abs() * (carry * multiplier / 10_000.0)
    return net.clip(lower=-1.0).astype(float)


def simulate_execution_aware_strategy(
    df: pd.DataFrame,
    target_position: pd.Series,
    atr_for_entry: pd.Series,
    *,
    stop_atr: float,
    take_profit_atr: float,
    transaction_cost_bps: float,
    carry_bps_per_bar: float | None = None,
    drawdown_limit: float | None = None,
    daily_loss_limit: float | None = None,
    loss_event_threshold: int = 0,
    cooldown_bars: int = 0,
    loss_lookback_bars: int = 24,
) -> dict[str, Any]:
    """Forward-only OHLC simulation using ATR fixed at the completed signal bar.

    target_position[i] is the desired exposure entering at open[i]. atr_for_entry[i]
    must be computed from information available before open[i], usually signals.atr.shift(1).
    Stops and targets persist for the life of a trade. If both barriers are touched in a
    bar, stop wins. A gap through a barrier is filled at the observed open, not at a better
    barrier price. A last bar with no next open earns no fabricated open-to-open return.
    """
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        if drawdown_limit is not None or daily_loss_limit is not None:
            raise ValueError("UTC-aware DatetimeIndex is required for daily risk halts")
    for name in ("open", "high", "low"):
        if name not in df.columns:
            raise ValueError(f"OHLC data missing required column {name!r}")
    opens = df["open"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    if not (np.isfinite(opens).all() and np.isfinite(highs).all() and np.isfinite(lows).all()):
        raise ValueError("OHLC prices must be finite")
    if (opens <= 0).any() or (lows <= 0).any() or (highs < lows).any():
        raise ValueError("OHLC prices must be positive and high must be at least low")
    if (opens < lows).any() or (opens > highs).any():
        raise ValueError("open prices must lie within each bar's low/high range")

    try:
        stop_mult = float(stop_atr)
        target_mult = float(take_profit_atr)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("stop and target ATR multipliers must be finite and positive") from exc
    if not math.isfinite(stop_mult) or stop_mult <= 0 or not math.isfinite(target_mult) or target_mult <= 0:
        raise ValueError("stop and target ATR multipliers must be finite and positive")
    transaction_bps = _finite_nonnegative(transaction_cost_bps, "transaction_cost_bps")
    carry = (
        None if carry_bps_per_bar is None
        else _finite_nonnegative(carry_bps_per_bar, "carry_bps_per_bar")
    )
    dd_limit = None if drawdown_limit is None else _finite_nonnegative(drawdown_limit, "drawdown_limit")
    day_limit = None if daily_loss_limit is None else _finite_nonnegative(daily_loss_limit, "daily_loss_limit")
    for name, value in (("drawdown_limit", dd_limit), ("daily_loss_limit", day_limit)):
        if value is not None and value > 1:
            raise ValueError(f"{name} must be at most 1")
    for name, value, minimum in (
        ("loss_event_threshold", loss_event_threshold, 0),
        ("cooldown_bars", cooldown_bars, 0),
        ("loss_lookback_bars", loss_lookback_bars, 1),
    ):
        if isinstance(value, bool) or int(value) != value or int(value) < minimum:
            raise ValueError(f"{name} must be an integer of at least {minimum}")

    positions = pd.Series(target_position, index=df.index).astype(float)
    atr = pd.Series(atr_for_entry, index=df.index).astype(float)
    if not np.isfinite(positions.to_numpy()).all():
        raise ValueError("target position contains a non-finite value")

    n = len(df)
    actual_positions = np.zeros(n, dtype=float)
    market_returns = np.zeros(n, dtype=float)
    exit_mask = np.zeros(n, dtype=bool)
    turnover = np.zeros(n, dtype=float)
    net_returns = np.zeros(n, dtype=float)
    stop_exits = take_exits = ambiguous_bars = gap_exits = 0
    active_sign = 0
    blocked_sign = 0
    stop_price: float | None = None
    target_price: float | None = None
    previous_position = 0.0
    equity = peak = 1.0
    day_start = 1.0
    current_day = None
    hard_halt = False
    daily_halt = False
    cooldown_until = -1
    force_flat_next = False
    halted_bars = 0
    loss_events: list[int] = []
    halt_reason: str | None = None
    halt_triggered_at: int | None = None
    cooldown_triggers = 0

    for i in range(n):
        timestamp = df.index[i]
        day = timestamp.tz_convert("UTC").date() if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None else timestamp.date()
        day_changed = day != current_day
        if day_changed:
            current_day = day
            day_start = equity
            daily_halt = False
            # Daily-loss halts end at the UTC-day boundary. Do not carry the stale
            # same-direction re-entry block into a new day; peak drawdown still latches.
            if not hard_halt and halt_reason == "DAILY_LOSS":
                blocked_sign = 0

        forced_flat = force_flat_next
        force_flat_next = False
        is_halted = forced_flat or hard_halt or daily_halt or i <= cooldown_until
        requested = 0.0 if is_halted else float(positions.iloc[i])
        sign = 1 if requested > 0 else -1 if requested < 0 else 0
        if is_halted:
            halted_bars += 1
            if active_sign and not (forced_flat and day_changed and not hard_halt):
                blocked_sign = active_sign
            active_sign = 0
            stop_price = target_price = None
        elif sign == 0:
            active_sign = 0
            blocked_sign = 0
            stop_price = target_price = None
        elif active_sign == 0:
            if sign == blocked_sign:
                requested = 0.0
                sign = 0
            else:
                entry_atr = float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else float("nan")
                if not math.isfinite(entry_atr) or entry_atr <= 0:
                    raise ValueError(f"non-zero position at {timestamp} has no finite entry ATR")
                active_sign = sign
                stop_price = opens[i] - sign * stop_mult * entry_atr
                target_price = opens[i] + sign * target_mult * entry_atr
                if stop_price <= 0:
                    raise ValueError(f"stop price is non-positive at {timestamp}")
        elif sign != active_sign:
            active_sign = 0
            stop_price = target_price = None
            if sign == blocked_sign:
                requested = 0.0
                sign = 0
            else:
                entry_atr = float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else float("nan")
                if not math.isfinite(entry_atr) or entry_atr <= 0:
                    raise ValueError(f"non-zero position at {timestamp} has no finite entry ATR")
                active_sign = sign
                stop_price = opens[i] - sign * stop_mult * entry_atr
                target_price = opens[i] + sign * target_mult * entry_atr
                if stop_price <= 0:
                    raise ValueError(f"stop price is non-positive at {timestamp}")

        actual = float(requested) if active_sign != 0 else 0.0
        bar_return = 0.0
        exited = False
        if actual != 0.0:
            if actual > 0:
                stop_hit = lows[i] <= float(stop_price)
                take_hit = highs[i] >= float(target_price)
            else:
                stop_hit = highs[i] >= float(stop_price)
                take_hit = lows[i] <= float(target_price)
            if stop_hit and take_hit:
                ambiguous_bars += 1
                take_hit = False
            if stop_hit:
                fill = opens[i] if (
                    (actual > 0 and opens[i] < float(stop_price))
                    or (actual < 0 and opens[i] > float(stop_price))
                ) else float(stop_price)
                bar_return = fill / opens[i] - 1.0
                stop_exits += 1
                exited = True
            elif take_hit:
                fill = opens[i] if (
                    (actual > 0 and opens[i] > float(target_price))
                    or (actual < 0 and opens[i] < float(target_price))
                ) else float(target_price)
                bar_return = fill / opens[i] - 1.0
                take_exits += 1
                exited = True
            elif i + 1 < n:
                next_open = opens[i + 1]
                gap_stop = (
                    (actual > 0 and next_open <= float(stop_price))
                    or (actual < 0 and next_open >= float(stop_price))
                )
                gap_target = (
                    (actual > 0 and next_open >= float(target_price))
                    or (actual < 0 and next_open <= float(target_price))
                )
                if gap_stop or gap_target:
                    bar_return = next_open / opens[i] - 1.0
                    exited = True
                    if gap_stop:
                        stop_exits += 1
                    else:
                        take_exits += 1
                    gap_exits += 1
                else:
                    bar_return = next_open / opens[i] - 1.0

        actual_positions[i] = actual
        market_returns[i] = float(bar_return)
        exit_mask[i] = exited
        turnover[i] = abs(actual - previous_position) + (abs(actual) if exited and actual != 0.0 else 0.0)
        step = actual * float(bar_return) - turnover[i] * transaction_bps / 10_000.0
        if carry is not None:
            step -= abs(actual) * carry / 10_000.0
        step = max(-1.0, float(step))
        net_returns[i] = step
        equity = max(0.0, equity * (1.0 + step))
        peak = max(peak, equity)
        drawdown = 1.0 - equity / peak if peak else 0.0
        daily_loss = 1.0 - equity / day_start if day_start else 0.0

        new_loss = actual != 0.0 and actual * float(bar_return) < 0.0
        if new_loss:
            loss_events.append(i)
        cutoff = i - int(loss_lookback_bars)
        loss_events = [event for event in loss_events if event >= cutoff]
        loss_cluster = (
            new_loss and int(loss_event_threshold) > 0
            and len(loss_events) >= int(loss_event_threshold)
        )
        if dd_limit is not None and drawdown >= dd_limit and not hard_halt:
            hard_halt = True
            halt_reason = "DRAWDOWN"
            halt_triggered_at = i
            force_flat_next = True
        elif day_limit is not None and daily_loss >= day_limit and not daily_halt:
            daily_halt = True
            if halt_reason is None:
                halt_reason = "DAILY_LOSS"
                halt_triggered_at = i
            force_flat_next = True
        elif not is_halted and loss_cluster:
            cooldown_until = max(cooldown_until, i + int(cooldown_bars))
            cooldown_triggers += 1

        if exited:
            blocked_sign = int(np.sign(actual))
            active_sign = 0
            stop_price = target_price = None
            previous_position = 0.0
        else:
            previous_position = actual

    position_series = pd.Series(actual_positions, index=df.index, dtype=float)
    return_series = pd.Series(market_returns, index=df.index, dtype=float)
    exit_series = pd.Series(exit_mask, index=df.index, dtype=bool)
    net_series = pd.Series(net_returns, index=df.index, dtype=float)
    turnover_series = pd.Series(turnover, index=df.index, dtype=float)
    audit = {
        "execution_model": "OHLC_INTRABAR_ATR_STOP_TAKE_PROFIT_NEXT_OPEN",
        "stop_loss_exits": stop_exits,
        "take_profit_exits": take_exits,
        "gap_exits": gap_exits,
        "ambiguous_bars_filled_stop_first": ambiguous_bars,
        "financing_cost_model_complete": carry is not None,
        "carry_bps_per_bar": carry,
        "transaction_cost_bps": transaction_bps,
        "risk_halted_bars": halted_bars,
        "risk_hard_halt": hard_halt,
        "risk_halt_reason": halt_reason,
        "risk_halt_triggered_at": halt_triggered_at,
        "risk_cooldown_until": cooldown_until,
        "risk_cooldown_triggers": cooldown_triggers,
        "final_equity": equity,
        "active_bars": int((position_series != 0).sum()),
        "net_returns_compounded": float(np.prod(1.0 + net_series.to_numpy()) if n else 1.0),
        "intrabar_stop_target_model_complete": True,
        "promotion_eligible_on_cost_model": carry is not None,
    }
    compounded = float(np.prod(1.0 + net_series.to_numpy())) if n else 1.0
    if not math.isclose(compounded, equity, rel_tol=1e-10, abs_tol=1e-12):
        raise RuntimeError("execution simulator equity differs from its returned net-return stream")
    return {
        "positions": position_series,
        "market_returns": return_series,
        "intrabar_exit": exit_series,
        "turnover": turnover_series,
        "net_returns": net_series,
        "audit": audit,
    }
