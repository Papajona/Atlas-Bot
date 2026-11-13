"""Execution-aware research simulation with intrabar stops, targets, and carry.

This module is offline research only. It never submits orders. Ambiguous OHLC bars are
resolved conservatively (stop before target). Financing is included only when an evidenced
per-bar rate is supplied; missing carry is exposed as incomplete research evidence.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

from .execution_costs import net_returns_after_costs


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
    position_values = positions.to_numpy(dtype=float)
    exit_values = exits.to_numpy(dtype=bool)
    out = np.zeros(len(position_values), dtype=float)
    previous = 0.0
    for i, current in enumerate(position_values):
        if not math.isfinite(float(current)):
            raise ValueError("position stream contains a non-finite value")
        out[i] = abs(float(current) - previous)
        if exit_values[i] and current != 0.0:
            out[i] += abs(float(current))
            previous = 0.0
        else:
            previous = float(current)
    return pd.Series(out, index=positions.index, dtype=float)

def costed_execution_returns(
    position: pd.Series,
    market_returns: pd.Series,
    *,
    transaction_cost_bps: float | pd.Series,
    carry_bps_per_bar: float | None,
    exit_mask: pd.Series | None = None,
    cost_multiplier: float = 1.0,
) -> pd.Series:
    """Compute net returns using transaction turnover and per-bar financing costs."""
    if isinstance(transaction_cost_bps, pd.Series):
        if not transaction_cost_bps.index.equals(pd.Series(position).index):
            raise ValueError("session transaction-cost series must align exactly with positions")
        cost_values = transaction_cost_bps.to_numpy(dtype=float)
        if not np.isfinite(cost_values).all() or np.any(cost_values < 0):
            raise ValueError("session transaction costs must be finite and non-negative")
    else:
        cost_values = _finite_nonnegative(transaction_cost_bps, "transaction_cost_bps")
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
    per_bar_costs = (
        cost_values
        if isinstance(cost_values, np.ndarray)
        else np.full(len(pos), float(cost_values), dtype=float)
    )
    carry_bps = 0.0 if carry is None else carry
    net_values = net_returns_after_costs(
        pos.to_numpy(dtype=float) * returns.to_numpy(dtype=float),
        turnover=turnover.to_numpy(dtype=float),
        execution_cost_bps=per_bar_costs,
        exposure=pos.to_numpy(dtype=float),
        financing_cost_bps_per_bar=carry_bps,
        cost_multiplier=multiplier,
    )
    return pd.Series(
        np.maximum(net_values, -1.0),
        index=pos.index,
        dtype=float,
        name="net_return",
    )


def simulate_execution_aware_strategy(
    df: pd.DataFrame,
    target_position: pd.Series,
    atr_for_entry: pd.Series,
    *,
    stop_atr: float,
    take_profit_atr: float,
    transaction_cost_bps: float,
    carry_bps_per_bar: float | None = None,
    session_costs_bps: dict[str, float] | None = None,
    transaction_cost_provider: Callable[[int, float, float, str, str], float] | None = None,
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
    barrier price. The last bar is liquidated at its observed close when no next open exists.
    """
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        if drawdown_limit is not None or daily_loss_limit is not None:
            raise ValueError("UTC-aware DatetimeIndex is required for daily risk halts")
    for name in ("open", "high", "low", "close"):
        if name not in df.columns:
            raise ValueError(f"OHLC data missing required column {name!r}")
    opens = df["open"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    if not (np.isfinite(opens).all() and np.isfinite(highs).all() and np.isfinite(lows).all() and np.isfinite(closes).all()):
        raise ValueError("OHLC prices must be finite")
    if (opens <= 0).any() or (lows <= 0).any() or (closes <= 0).any() or (highs < lows).any():
        raise ValueError("OHLC prices must be positive and high must be at least low")
    if (opens < lows).any() or (opens > highs).any() or (closes < lows).any() or (closes > highs).any():
        raise ValueError("open prices must lie within each bar's low/high range")

    try:
        stop_mult = float(stop_atr)
        target_mult = float(take_profit_atr)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("stop and target ATR multipliers must be finite and positive") from exc
    if not math.isfinite(stop_mult) or stop_mult <= 0 or not math.isfinite(target_mult) or target_mult <= 0:
        raise ValueError("stop and target ATR multipliers must be finite and positive")
    transaction_bps = _finite_nonnegative(transaction_cost_bps, "transaction_cost_bps")
    if transaction_cost_provider is not None and session_costs_bps is not None:
        raise ValueError("event-level transaction costs cannot be combined with session schedules")
    session_schedule = None
    per_bar_transaction_bps = np.full(len(df), transaction_bps, dtype=float)
    if session_costs_bps is not None:
        from .market_sessions import normalize_session_costs_bps, session_cost_series
        session_schedule = normalize_session_costs_bps(session_costs_bps)
        per_bar_transaction_bps = session_cost_series(df.index, session_schedule).to_numpy(dtype=float)
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

    if isinstance(target_position, pd.Series):
        positions = (
            target_position.to_numpy(dtype=float, copy=False)
            if target_position.index.equals(df.index)
            else target_position.reindex(df.index).to_numpy(dtype=float)
        )
    else:
        positions = np.asarray(target_position, dtype=float)
    if isinstance(atr_for_entry, pd.Series):
        atr = (
            atr_for_entry.to_numpy(dtype=float, copy=False)
            if atr_for_entry.index.equals(df.index)
            else atr_for_entry.reindex(df.index).to_numpy(dtype=float)
        )
    else:
        atr = np.asarray(atr_for_entry, dtype=float)
    n = len(df)
    if positions.shape != (n,) or atr.shape != (n,):
        raise ValueError("target position and entry ATR must align with the OHLC rows")
    if not np.isfinite(positions).all():
        raise ValueError("target position contains a non-finite value")
    actual_positions = np.zeros(n, dtype=float)
    market_returns = np.zeros(n, dtype=float)
    exit_mask = np.zeros(n, dtype=bool)
    turnover = np.zeros(n, dtype=float)
    transaction_cost_drag = np.zeros(n, dtype=float)
    effective_transaction_cost_bps = per_bar_transaction_bps.copy()
    net_returns = np.zeros(n, dtype=float)
    stop_exits = take_exits = ambiguous_bars = gap_exits = terminal_close_exits = 0
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
    if isinstance(df.index, pd.DatetimeIndex):
        utc_days = df.index.tz_convert("UTC").date if df.index.tz is not None else df.index.date
    else:
        utc_days = np.asarray([timestamp.date() for timestamp in df.index], dtype=object)

    for i in range(n):
        timestamp = df.index[i]
        day = utc_days[i]
        day_changed = day != current_day
        if day_changed:
            current_day = day
            day_start = equity
            # Daily accounting resets at UTC midnight, but a triggered risk halt remains
            # latched for this simulation. Live/customer-account controls require manual
            # intervention; research must not model automatic re-entry as safer behavior.

        forced_flat = force_flat_next
        force_flat_next = False
        is_halted = forced_flat or hard_halt or daily_halt or i <= cooldown_until
        requested = 0.0 if is_halted else float(positions[i])
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
                entry_atr = float(atr[i]) if np.isfinite(atr[i]) else float("nan")
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
                entry_atr = float(atr[i]) if np.isfinite(atr[i]) else float("nan")
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
        gap_exit_fill = False
        exit_fill_price: float | None = None
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
                exit_fill_price = float(fill)
                stop_exits += 1
                exited = True
            elif take_hit:
                fill = opens[i] if (
                    (actual > 0 and opens[i] > float(target_price))
                    or (actual < 0 and opens[i] < float(target_price))
                ) else float(target_price)
                bar_return = fill / opens[i] - 1.0
                exit_fill_price = float(fill)
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
                    exit_fill_price = float(next_open)
                    exited = True
                    gap_exit_fill = True
                    if gap_stop:
                        stop_exits += 1
                    else:
                        take_exits += 1
                    gap_exits += 1
                else:
                    bar_return = next_open / opens[i] - 1.0
            else:
                # The end of an evaluated window is a liquidation boundary. Use
                # its observed close rather than zero or a made-up future open.
                bar_return = closes[i] / opens[i] - 1.0
                exit_fill_price = float(closes[i])
                exited = True
                terminal_close_exits += 1

        actual_positions[i] = actual
        market_returns[i] = float(bar_return)
        exit_mask[i] = exited
        entry_turnover = abs(actual - previous_position)
        exit_turnover = abs(actual) if exited and actual != 0.0 else 0.0
        turnover[i] = entry_turnover + exit_turnover
        exit_cost_index = i + 1 if gap_exit_fill and i + 1 < n else i
        if transaction_cost_provider is None:
            entry_cost_bps = float(per_bar_transaction_bps[i])
            exit_cost_bps = float(per_bar_transaction_bps[exit_cost_index])
        else:
            entry_cost_bps = 0.0
            exit_cost_bps = 0.0
            if entry_turnover > 0.0:
                entry_side = "buy" if actual - previous_position > 0.0 else "sell"
                entry_cost_bps = float(transaction_cost_provider(
                    i, entry_turnover, float(opens[i]), entry_side, "ENTRY"
                ))
            if exit_turnover > 0.0:
                if exit_fill_price is None:
                    raise RuntimeError("execution exit was flagged without a verified fill price")
                exit_side = "sell" if actual > 0.0 else "buy"
                exit_reference_price = float(opens[exit_cost_index]) if gap_exit_fill else exit_fill_price
                exit_cost_bps = float(transaction_cost_provider(
                    exit_cost_index, exit_turnover, exit_reference_price, exit_side, "EXIT"
                ))
            if (
                not math.isfinite(entry_cost_bps)
                or entry_cost_bps < 0.0
                or not math.isfinite(exit_cost_bps)
                or exit_cost_bps < 0.0
            ):
                raise ValueError("event-level transaction cost provider returned an invalid rate")
        entry_cost_drag = entry_turnover * entry_cost_bps / 10_000.0
        exit_cost_drag = exit_turnover * exit_cost_bps / 10_000.0
        transaction_cost_drag[i] = entry_cost_drag + exit_cost_drag
        if turnover[i] > 0.0:
            effective_transaction_cost_bps[i] = (
                transaction_cost_drag[i] * 10_000.0 / turnover[i]
            )
        step = actual * float(bar_return) - transaction_cost_drag[i]
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
    transaction_cost_drag_series = pd.Series(transaction_cost_drag, index=df.index, dtype=float)
    effective_transaction_cost_series = pd.Series(
        effective_transaction_cost_bps, index=df.index, dtype=float
    )
    audit = {
        "execution_model": "OHLC_INTRABAR_ATR_STOP_TAKE_PROFIT_NEXT_OPEN",
        "stop_loss_exits": stop_exits,
        "take_profit_exits": take_exits,
        "gap_exits": gap_exits,
        "terminal_close_exits": terminal_close_exits,
        "ambiguous_bars_filled_stop_first": ambiguous_bars,
        "financing_cost_model_complete": carry is not None,
        "carry_bps_per_bar": carry,
        "transaction_cost_bps": transaction_bps,
        "transaction_cost_drag": float(transaction_cost_drag.sum()),
        "gap_exit_fee_uses_fill_session": True,
        "session_cost_model_complete": session_schedule is not None,
        "session_costs_bps": session_schedule,
        "per_bar_execution_cost_model_complete": transaction_cost_provider is not None,
        "per_bar_execution_cost_model_status": (
            "EVENT_LEVEL_PROVIDER_APPLIED"
            if transaction_cost_provider is not None
            else "STATIC_OR_SESSION_COSTS"
        ),
        "session_cost_observations": int(len(per_bar_transaction_bps)) if session_schedule is not None else 0,
        "risk_halted_bars": halted_bars,
        "risk_hard_halt": hard_halt,
        "risk_halt_reason": halt_reason,
        "risk_halt_triggered_at": halt_triggered_at,
        "risk_cooldown_until": cooldown_until,
        "risk_cooldown_triggers": cooldown_triggers,
        "final_equity": equity,
        "active_bars": int((position_series != 0).sum()),
        "net_returns_compounded": float(np.prod(1.0 + net_series.to_numpy()) if n else 1.0),
        "intrabar_stop_target_model_complete": False,  # OHLC cannot prove tick-level event order
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
        "transaction_cost_drag": transaction_cost_drag_series,
        "effective_transaction_cost_bps": effective_transaction_cost_series,
        "net_returns": net_series,
        "audit": audit,
    }
