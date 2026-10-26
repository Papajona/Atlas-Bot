from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable
import math
import numpy as np
import pandas as pd
from .strategy_engine import next_open_returns, _annualization_factor
from .execution_aware_research import (
    costed_execution_returns, execution_turnover, simulate_execution_aware_strategy,
)


@dataclass(frozen=True)
class WalkForwardFold:
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    purge: int
    embargo: int


def validate_ohlcv_frame(
    df: pd.DataFrame,
    require_volume: bool = False,
    *,
    timeframe: str | None = None,
    asset: str | None = None,
) -> dict:
    required = {"open", "high", "low", "close"}
    missing = sorted(required - set(df.columns))
    issues: list[str] = []
    if missing:
        issues.append("missing_columns:" + ",".join(missing))
    if not isinstance(df.index, pd.DatetimeIndex):
        issues.append("index_not_datetime")
    else:
        if not df.index.is_monotonic_increasing:
            issues.append("index_not_monotonic")
        if df.index.has_duplicates:
            issues.append("duplicate_timestamps")
    if require_volume and "volume" not in df.columns:
        issues.append("missing_volume")

    attrs = getattr(df, "attrs", {}) or {}
    provenance = attrs.get("data_provenance") if isinstance(attrs.get("data_provenance"), dict) else {}
    raw_timeframe = timeframe or attrs.get("timeframe") or provenance.get("timeframe")
    raw_asset = str(asset or attrs.get("asset_class") or provenance.get("asset_class") or "").strip().lower()
    expected_seconds = None
    if raw_timeframe:
        try:
            parsed = float(pd.Timedelta(str(raw_timeframe)).total_seconds())
            if math.isfinite(parsed) and parsed > 0:
                expected_seconds = parsed
        except (TypeError, ValueError, OverflowError):
            expected_seconds = None

    gap_report = {
        "interval_count": max(0, len(df.index) - 1),
        "expected_timeframe": str(raw_timeframe) if raw_timeframe else None,
        "expected_interval_seconds": expected_seconds,
        "intervals_above_1_5x_expected": None,
        "intervals_above_1_5x_median": 0,
        "unexpected_gap_intervals": None,
        "estimated_missing_bars": None,
        "market_closure_suspected_intervals": 0,
        "max_interval_multiple": None,
        "covered_interval_ratio": None,
        "method": "expected-timeframe gap estimate; >36-hour non-crypto closures are reported separately and are not rejected",
    }
    if isinstance(df.index, pd.DatetimeIndex) and len(df.index) >= 2:
        intervals = df.index.to_series().diff().dt.total_seconds().to_numpy(dtype=float)[1:]
        intervals = intervals[np.isfinite(intervals) & (intervals > 0)]
        if len(intervals):
            median_seconds = float(np.median(intervals))
            cadence = expected_seconds if expected_seconds is not None else median_seconds
            if cadence > 0:
                multiples_expected = intervals / cadence
                multiples_median = intervals / median_seconds if median_seconds > 0 else np.ones(len(intervals))
                above_expected = multiples_expected > 1.5
                above_median = multiples_median > 1.5
                # Weekend/long market closures are a separate diagnostic for FX and
                # commodity data: a large calendar interval must not be mistaken for
                # a missing run of tradable bars without calendar evidence.
                closure_suspected = (
                    (intervals >= 36 * 3600)
                    if raw_asset in {"forex", "fx", "commodity", "commodities"}
                    else np.zeros(len(intervals), dtype=bool)
                )
                unexpected = above_expected & ~closure_suspected
                estimated_missing = int(np.sum(
                    np.maximum(0, np.floor(multiples_expected[unexpected]).astype(int) - 1)
                ))
                expected_gaps = int(np.count_nonzero(above_expected))
                unexpected_count = int(np.count_nonzero(unexpected))
                gap_report.update({
                    "intervals_above_1_5x_expected": expected_gaps,
                    "intervals_above_1_5x_median": int(np.count_nonzero(above_median)),
                    "unexpected_gap_intervals": unexpected_count,
                    "estimated_missing_bars": estimated_missing,
                    "market_closure_suspected_intervals": int(np.count_nonzero(closure_suspected & above_expected)),
                    "max_interval_multiple": float(np.max(multiples_expected)),
                    "covered_interval_ratio": float(
                        len(intervals) / (len(intervals) + estimated_missing)
                    ) if len(intervals) + estimated_missing else 1.0,
                    "median_observed_interval_seconds": median_seconds,
                    "cadence_source": "declared_timeframe" if expected_seconds is not None else "observed_median_fallback",
                })
    if not missing:
        numeric = df[list(required)].apply(pd.to_numeric, errors="coerce")
        if numeric.isna().any().any():
            issues.append("non_numeric_or_nan_ohlc")
        # Some vendors reconstruct/open-adjust bars; enforce the core high/low/close
        # envelope while reporting open anomalies separately rather than rejecting
        # an otherwise usable series outright.
        bad_range = (numeric["high"] < numeric[["close", "low"]].max(axis=1)) | (numeric["low"] > numeric[["close", "high"]].min(axis=1))
        if bool(bad_range.fillna(False).any()):
            issues.append("invalid_ohlc_range")
        if bool((numeric <= 0).any().any()):
            issues.append("non_positive_ohlc")
    return {"valid": not issues, "issues": issues, "rows": int(len(df)), "gap_report": gap_report}


def purged_walk_forward_splits(n: int, folds: int = 5, min_train: int = 800,
                               test_size: int | None = None, purge: int = 1,
                               embargo: int = 1) -> list[WalkForwardFold]:
    if n <= 0 or folds <= 0 or min_train <= 0:
        return []
    if test_size is None:
        remaining = max(0, n - min_train)
        test_size = max(1, remaining // folds) if remaining else 0
    if test_size <= 0:
        return []
    out: list[WalkForwardFold] = []
    train_end = min_train
    for _ in range(folds):
        test_start = train_end + max(0, purge)
        test_end = min(n, test_start + test_size)
        if test_end <= test_start:
            break
        out.append(WalkForwardFold(0, train_end, test_start, test_end, purge, embargo))
        train_end = min(n, test_end + max(0, embargo))
        if train_end >= n:
            break
    return out


def bootstrap_mean_ci(returns: Iterable[float], samples: int = 2000,
                      seed: int = 17, alpha: float = 0.05,
                      block_size: int = 1) -> dict:
    x = np.asarray(list(returns), dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 20:
        return {"status": "INSUFFICIENT_DATA", "n": int(len(x))}
    rng = np.random.default_rng(seed)
    if block_size <= 1:
        draws = rng.integers(0, len(x), size=(samples, len(x)))
        means = x[draws].mean(axis=1)
    else:
        blocks = [x[i:i + block_size] for i in range(0, len(x) - block_size + 1)]
        means = np.empty(samples, dtype=float)
        for j in range(samples):
            chosen = []
            while len(chosen) < len(x):
                chosen.extend(blocks[int(rng.integers(0, len(blocks)))])
            means[j] = np.mean(chosen[:len(x)])
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return {"status": "OK", "n": int(len(x)), "mean": float(x.mean()), "ci_low": float(lo), "ci_high": float(hi), "alpha": alpha}


def newey_west_mean_inference(returns: Iterable[float], *, max_lags: int | None = None) -> dict:
    """HAC inference for the mean net per-period return, with effective-sample diagnostics.

    This is a one-sided test of positive mean return. The annualized Sharpe is not
    treated as a significance test. The effective sample is an autocorrelation-aware
    diagnostic, not a count of independent observations proven by the data.
    """
    x = np.asarray(list(returns), dtype=float)
    x = x[np.isfinite(x)]
    n = int(len(x))
    if n < 30:
        return {"status": "INSUFFICIENT_DATA", "observations": n}
    mean = float(np.mean(x))
    centred = x - mean
    variance = float(np.dot(centred, centred) / n)
    if not math.isfinite(variance) or variance <= 0:
        return {"status": "ZERO_OR_INVALID_VARIANCE", "observations": n, "mean_return_per_period": mean}
    if max_lags is None:
        max_lags = int(math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))
    lags = max(0, min(int(max_lags), n - 1))
    long_run_variance = variance
    for lag in range(1, lags + 1):
        gamma = float(np.dot(centred[lag:], centred[:-lag]) / n)
        weight = 1.0 - lag / (lags + 1.0)
        long_run_variance += 2.0 * weight * gamma
    if not math.isfinite(long_run_variance) or long_run_variance <= 0:
        return {"status": "INVALID_LONG_RUN_VARIANCE", "observations": n, "max_lags": lags}
    se = math.sqrt(long_run_variance / n)
    t_stat = mean / se if se > 0 else None
    effective_n = int(max(1, min(n, round(n * variance / long_run_variance))))
    p_one_sided = 0.5 * math.erfc(float(t_stat) / math.sqrt(2.0)) if t_stat is not None else None
    std = float(np.std(x, ddof=1))
    return {
        "status": "OK",
        "observations": n,
        "effective_observations": effective_n,
        "mean_return_per_period": mean,
        "per_period_sharpe": float(mean / std) if std > 0 else None,
        "newey_west_standard_error": se,
        "newey_west_t_statistic": float(t_stat) if t_stat is not None else None,
        "one_sided_normal_p_value": float(p_one_sided) if p_one_sided is not None else None,
        "max_lags": lags,
        "inference": "Newey-West HAC mean-return test; normal-tail approximation; use as a gate only alongside trial deflation and minimum effective sample",
    }


def oos_benchmark_comparison(
    ohlc_blocks: list[pd.DataFrame],
    entry_signal_blocks: list[Iterable[float]],
    candidate_net_blocks: list[Iterable[float]],
    entry_atr_blocks: list[Iterable[float]] | None = None,
    *,
    transaction_cost_bps: float,
    carry_bps_per_bar: float | None,
    stop_atr: float | None = None,
    take_profit_atr: float | None = None,
    drawdown_limit: float | None = None,
    daily_loss_limit: float | None = None,
    loss_event_threshold: int = 0,
    cooldown_bars: int = 0,
    loss_lookback_bars: int = 24,
    random_trials: int = 100,
    seed: int = 20261026,
) -> dict:
    """Compare candidate returns with OHLC-derived buy-and-hold and fresh-entry controls.

    Market returns are independently derived from fold OHLC open-to-open moves, with
    the final interval liquidated at the observed close. Random-entry controls shift the
    *requested entry stream* without circular wraparound and are each simulated from flat
    with their own stops, risk halts, exit events, transaction costs, carry and terminal
    liquidation. Candidate fills and exit-conditioned returns are never reused as controls.
    """
    if not (
        len(ohlc_blocks) == len(entry_signal_blocks) == len(candidate_net_blocks)
        and ohlc_blocks
        and (entry_atr_blocks is None or len(entry_atr_blocks) == len(ohlc_blocks))
    ):
        return {"status": "INSUFFICIENT_DATA", "reason": "fold_block_mismatch"}
    try:
        cost_rate = float(transaction_cost_bps) / 10000.0
        carry_rate = None if carry_bps_per_bar is None else float(carry_bps_per_bar) / 10000.0
        if not math.isfinite(cost_rate) or cost_rate < 0:
            raise ValueError
        if carry_rate is not None and (not math.isfinite(carry_rate) or carry_rate < 0):
            raise ValueError
        bracket_configured = stop_atr is not None or take_profit_atr is not None
        if bracket_configured:
            if stop_atr is None or take_profit_atr is None:
                raise ValueError
            if not math.isfinite(float(stop_atr)) or float(stop_atr) <= 0:
                raise ValueError
            if not math.isfinite(float(take_profit_atr)) or float(take_profit_atr) <= 0:
                raise ValueError
    except (TypeError, ValueError, OverflowError):
        return {"status": "INVALID_CONFIG", "reason": "invalid_cost_or_exit_config"}

    market_blocks: list[np.ndarray] = []
    signals: list[np.ndarray] = []
    candidate_blocks: list[np.ndarray] = []
    atr_blocks: list[np.ndarray] = []
    frame_blocks: list[pd.DataFrame] = []
    for index, (frame, entry_signal, candidate_net) in enumerate(
        zip(ohlc_blocks, entry_signal_blocks, candidate_net_blocks)
    ):
        if not isinstance(frame, pd.DataFrame) or frame.empty or not {"open", "high", "low", "close"} <= set(frame.columns):
            return {"status": "INSUFFICIENT_DATA", "reason": "invalid_ohlc_fold"}
        if not isinstance(frame.index, pd.DatetimeIndex) or not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
            return {"status": "INVALID_DATA", "reason": "invalid_ohlc_index"}
        try:
            opens = frame["open"].to_numpy(dtype=float)
            highs = frame["high"].to_numpy(dtype=float)
            lows = frame["low"].to_numpy(dtype=float)
            closes = frame["close"].to_numpy(dtype=float)
            entry = np.asarray(list(entry_signal), dtype=float)
            candidate = np.asarray(list(candidate_net), dtype=float)
            atr = (
                np.asarray(list(entry_atr_blocks[index]), dtype=float)
                if entry_atr_blocks is not None else np.full(len(frame), np.nan, dtype=float)
            )
        except (TypeError, ValueError, OverflowError):
            return {"status": "INVALID_DATA", "reason": "nonnumeric_benchmark_inputs"}
        if (
            len(entry) != len(frame) or len(candidate) != len(frame) or len(atr) != len(frame)
            or not all(np.isfinite(v).all() for v in (opens, highs, lows, closes, entry, candidate))
            or (opens <= 0).any() or (closes <= 0).any()
            or (highs < lows).any() or (opens < lows).any() or (opens > highs).any()
            or (closes < lows).any() or (closes > highs).any()
        ):
            return {"status": "INVALID_DATA", "reason": "nonfinite_or_misaligned_benchmark_inputs"}
        if bracket_configured and (not np.isfinite(atr).all() or (atr[entry != 0.0] <= 0).any()):
            return {"status": "INVALID_DATA", "reason": "invalid_entry_atr_for_shifted_control"}
        # Each row earns open[i] -> open[i+1]. The terminal row uses its observed
        # close; never infer market movement from the candidate's stop/exit stream.
        market = np.empty(len(frame), dtype=float)
        if len(frame) > 1:
            market[:-1] = opens[1:] / opens[:-1] - 1.0
        market[-1] = closes[-1] / opens[-1] - 1.0
        market_blocks.append(market)
        signals.append(entry)
        candidate_blocks.append(candidate)
        atr_blocks.append(atr)
        frame_blocks.append(frame)

    candidate_return = float(
        np.prod(np.concatenate([1.0 + np.clip(values, -1.0, None) for values in candidate_blocks])) - 1.0
    )
    buy_hold_values = []
    buy_hold_turnover = 0.0
    for market in market_blocks:
        turnover = np.zeros(len(market), dtype=float)
        turnover[0] = 1.0  # enter from flat at fold open
        turnover[-1] += 1.0  # liquidate at the observed terminal close
        buy_hold_turnover += float(turnover.sum())
        net = market - turnover * cost_rate
        if carry_rate is not None:
            net = net - carry_rate
        buy_hold_values.extend(np.clip(net, -1.0, None).tolist())
    buy_hold_return = float(np.prod(1.0 + np.asarray(buy_hold_values, dtype=float)) - 1.0)

    rng = np.random.default_rng(seed)
    random_returns: list[float] = []
    random_trade_counts: list[int] = []
    random_turnover_totals: list[float] = []
    random_exit_counts: list[int] = []
    for _ in range(max(1, int(random_trials))):
        trial_values: list[float] = []
        trade_count = 0
        turnover_total = 0.0
        exit_count = 0
        for frame, market, original_signal, atr in zip(frame_blocks, market_blocks, signals, atr_blocks):
            # Non-circular shift: the discarded prefix remains flat, no positions
            # wrap from the end of a fold into its beginning.
            shift = int(rng.integers(1, len(original_signal))) if len(original_signal) > 1 else 0
            shifted = np.zeros_like(original_signal)
            shifted_atr = np.full_like(atr, np.nan, dtype=float)
            if shift < len(shifted):
                shifted[shift:] = original_signal[:len(shifted) - shift]
                shifted_atr[shift:] = atr[:len(shifted_atr) - shift]
            if bracket_configured:
                simulated = simulate_execution_aware_strategy(
                    frame,
                    pd.Series(shifted, index=frame.index, dtype=float),
                    pd.Series(shifted_atr, index=frame.index, dtype=float),
                    stop_atr=float(stop_atr),
                    take_profit_atr=float(take_profit_atr),
                    transaction_cost_bps=float(transaction_cost_bps),
                    carry_bps_per_bar=carry_bps_per_bar,
                    drawdown_limit=drawdown_limit,
                    daily_loss_limit=daily_loss_limit,
                    loss_event_threshold=loss_event_threshold,
                    cooldown_bars=cooldown_bars,
                    loss_lookback_bars=loss_lookback_bars,
                )
                net = simulated["net_returns"].to_numpy(dtype=float)
                turnover = simulated["turnover"].to_numpy(dtype=float)
                exits = simulated["intrabar_exit"].to_numpy(dtype=bool)
                executed = simulated["positions"].to_numpy(dtype=float)
                trade_count += int(np.count_nonzero((executed != 0.0) & (np.r_[0.0, executed[:-1]] == 0.0)))
                turnover_total += float(turnover.sum())
                exit_count += int(np.count_nonzero(exits))
            else:
                previous = np.r_[0.0, shifted[:-1]]
                turnover = np.abs(shifted - previous)
                turnover[-1] += abs(float(shifted[-1]))  # terminal liquidation
                net = shifted * market - turnover * cost_rate
                if carry_rate is not None:
                    net = net - np.abs(shifted) * carry_rate
                net = np.clip(net, -1.0, None)
                trade_count += int(np.count_nonzero((shifted != 0.0) & ((previous == 0.0) | (np.sign(shifted) != np.sign(previous)))))
                turnover_total += float(turnover.sum())
                exit_count += int(np.count_nonzero((shifted != 0.0) & (np.r_[shifted[1:], 0.0] == 0.0)))
            trial_values.extend(np.asarray(net, dtype=float).tolist())
        random_returns.append(float(np.prod(1.0 + np.asarray(trial_values, dtype=float)) - 1.0))
        random_trade_counts.append(trade_count)
        random_turnover_totals.append(turnover_total)
        random_exit_counts.append(exit_count)

    random_median = float(np.median(random_returns))
    random_p95 = float(np.quantile(random_returns, 0.95))
    return {
        "status": "OK",
        "candidate_net_return": candidate_return,
        "buy_and_hold_net_return": buy_hold_return,
        "excess_return_vs_buy_and_hold": candidate_return - buy_hold_return,
        "matched_random_entry_median_net_return": random_median,
        "matched_random_entry_p95_net_return": random_p95,
        "excess_return_vs_random_p95": candidate_return - random_p95,
        "random_trials": len(random_returns),
        "random_control_trade_count_median": float(np.median(random_trade_counts)),
        "random_control_turnover_median": float(np.median(random_turnover_totals)),
        "random_control_exit_count_median": float(np.median(random_exit_counts)),
        "buy_and_hold_turnover": buy_hold_turnover,
        "benchmark_market_return_source": "OHLC_OPEN_TO_OPEN_WITH_TERMINAL_CLOSE",
        "random_control_execution_model": "INDEPENDENT_NONCIRCULAR_ENTRY_SHIFT_WITH_FLAT_START_AND_TERMINAL_LIQUIDATION",
        "equal_weight_basket": {"status": "UNAVAILABLE_SINGLE_SYMBOL_INPUT"},
        "gate_passed": candidate_return > buy_hold_return and candidate_return > random_p95,
        "cost_assumptions": {"transaction_cost_bps_per_turnover": float(transaction_cost_bps), "carry_bps_per_bar": carry_bps_per_bar},
        "note": "Market returns come from raw fold OHLC. Each shifted entry control starts flat and charges its own turnover, exits, carry, risk halts and terminal liquidation.",
    }


def multiple_testing_sharpe_adjustment(sharpes: Iterable[float], trials: int) -> dict:
    s = np.asarray([x for x in sharpes if np.isfinite(x)], dtype=float)
    if len(s) < 2:
        return {"status": "INSUFFICIENT_TRIAL_DISPERSION",
                "observed_trials": int(len(s)), "requested_trials": int(trials)}
    # Use the expected-maximum helper with the observed trial variance. Inputs and
    # output remain in the same Sharpe units. This remains a screening diagnostic,
    # not the promotion-grade Deflated Sharpe Ratio.
    from .research_stats import expected_max_sharpe
    m = max(1, int(trials))
    dispersion_variance = float(np.var(s, ddof=1))
    if not math.isfinite(dispersion_variance) or dispersion_variance <= 0:
        return {"status": "INSUFFICIENT_TRIAL_DISPERSION",
                "observed_trials": int(len(s)), "requested_trials": m}
    expected_max = float(expected_max_sharpe(m, dispersion_variance))
    best = float(np.max(s))
    adjusted = best - expected_max
    return {"status": "OK", "best_sharpe": best, "trials": m,
            "observed_trial_variance": dispersion_variance,
            "expected_null_max_proxy": expected_max, "adjusted_sharpe_proxy": float(adjusted),
            "note": "Dispersion-scaled screening proxy only; not a formal Deflated Sharpe Ratio."}


def cost_sensitivity(returns_gross: Iterable[float], turnover: Iterable[float],
                     cost_bps_grid: Iterable[float] = (2, 5, 10, 20, 40)) -> list[dict]:
    r = np.asarray(list(returns_gross), dtype=float)
    t = np.asarray(list(turnover), dtype=float)
    n = min(len(r), len(t))
    if n == 0:
        return []
    r, t = r[:n], t[:n]
    out = []
    for bps in cost_bps_grid:
        net = r - t * float(bps) / 10_000.0
        out.append({"cost_bps": float(bps), "total_return": float(np.prod(1 + net) - 1),
                    "mean_return": float(np.nanmean(net)), "positive_mean": bool(np.nanmean(net) > 0)})
    return out


def ohlc_trade_ambiguity(df: pd.DataFrame, stop: pd.Series, target: pd.Series,
                         side: pd.Series) -> dict:
    ambiguous = 0
    checked = 0
    for idx in df.index:
        try:
            lo, hi = float(df.at[idx, "low"]), float(df.at[idx, "high"])
            st, tp, s = float(stop.at[idx]), float(target.at[idx]), str(side.at[idx])
        except Exception:
            continue
        if s not in {"buy", "sell"} or not np.isfinite(st) or not np.isfinite(tp):
            continue
        checked += 1
        if s == "buy":
            hit_stop, hit_target = lo <= st, hi >= tp
        else:
            hit_stop, hit_target = hi >= st, lo <= tp
        if hit_stop and hit_target:
            ambiguous += 1
    return {"checked": checked, "ambiguous": ambiguous,
            "ambiguity_rate": float(ambiguous / checked) if checked else 0.0,
            "policy": "WORST_CASE" if ambiguous else "NONE"}


def fixed_signal_walk_forward_oos(
    df: pd.DataFrame,
    signal: pd.Series,
    costs_bps: float = 7.5,
    folds: int = 5,
    min_train: int = 800,
    annualization_factor: float = 252.0,
    taker_bps: float | None = None,
    slippage_bps: float | None = None,
    cost_stress_multiplier: float = 2.0,
    positions_are_final: bool = False,
    bar_returns: pd.Series | None = None,
    intrabar_exit: pd.Series | None = None,
    carry_bps_per_bar: float | None = None,
    carry_evidence_source: str | None = None,
    carry_evidence_id: str | None = None,
    max_hold_bars: int | None = None,
    embargo_bars: int | None = None,
    benchmark_entry_signal: pd.Series | None = None,
    benchmark_atr_for_entry: pd.Series | None = None,
    benchmark_stop_atr: float | None = None,
    benchmark_take_profit_atr: float | None = None,
    benchmark_drawdown_limit: float | None = None,
    benchmark_daily_loss_limit: float | None = None,
    benchmark_loss_event_threshold: int = 0,
    benchmark_cooldown_bars: int = 0,
) -> dict:
    """Evaluate held-out returns with the same executable position/stop/carry stream.

    With bar_returns and intrabar_exit supplied, the stream comes from the OHLC execution
    simulator and positions_are_final must be true. Without them the function remains a
    legacy next-open diagnostic and reports intrabar parity as incomplete. Missing carry
    is reported as incomplete evidence rather than silently certified as zero financing.
    """
    purge_bars = max(1, int(max_hold_bars)) if max_hold_bars is not None else 1
    effective_embargo = max(purge_bars, int(embargo_bars)) if embargo_bars is not None else purge_bars
    splits = purged_walk_forward_splits(
        len(df), folds=folds, min_train=min_train, purge=purge_bars, embargo=effective_embargo
    )
    if not splits:
        return {"status": "INSUFFICIENT_DATA", "folds": []}
    try:
        annualization = float(annualization_factor)
        total_cost_bps = float(costs_bps)
        effective_taker_bps = total_cost_bps if taker_bps is None else float(taker_bps)
        effective_slippage_bps = 0.0 if slippage_bps is None else float(slippage_bps)
        stress_multiplier = float(cost_stress_multiplier)
        carry = None if carry_bps_per_bar is None else float(carry_bps_per_bar)
    except (TypeError, ValueError, OverflowError):
        return {"status": "INVALID_CONFIG", "reason": "invalid_numeric_config", "folds": []}
    if not math.isfinite(annualization) or annualization <= 0:
        return {"status": "INVALID_CONFIG", "reason": "invalid_annualization_factor", "folds": []}
    if (
        not math.isfinite(total_cost_bps) or total_cost_bps < 0
        or not math.isfinite(effective_taker_bps) or effective_taker_bps < 0
        or not math.isfinite(effective_slippage_bps) or effective_slippage_bps < 0
        or not math.isclose(effective_taker_bps + effective_slippage_bps, total_cost_bps, abs_tol=1e-8)
        or not math.isfinite(stress_multiplier) or stress_multiplier <= 0
        or (carry is not None and (not math.isfinite(carry) or carry < 0))
    ):
        return {"status": "INVALID_CONFIG", "reason": "invalid_cost_config", "folds": []}

    # A precomputed signal with a different index can silently reindex to NaN and
    # then be converted to flat positions. That masks alignment/data-window bugs and
    # can make an evaluation appear valid despite signals being attached to the wrong
    # bars. Require exact index identity before any warm-up NaNs are treated as flat.
    if isinstance(signal, pd.Series) and not signal.index.equals(df.index):
        return {
            "status": "INVALID_DATA",
            "reason": "signal_index_mismatch",
            "folds": [],
        }
    if len(signal) != len(df):
        return {
            "status": "INVALID_DATA",
            "reason": "signal_length_mismatch",
            "folds": [],
        }
    s = pd.Series(signal, index=df.index).astype(float).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    position = s if positions_are_final else s.shift(1).fillna(0.0)
    if (bar_returns is not None or intrabar_exit is not None) and not positions_are_final:
        return {"status": "INVALID_CONFIG", "reason": "execution_stream_requires_final_positions", "folds": []}
    if bar_returns is None:
        ret = (next_open_returns(df) - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    else:
        ret = pd.Series(bar_returns, index=df.index).astype(float).replace([np.inf, -np.inf], np.nan)
        if ret.isna().any():
            return {"status": "INVALID_DATA", "reason": "bar_returns_contains_missing_or_nonfinite_values", "folds": []}
    exit_flags = None if intrabar_exit is None else pd.Series(intrabar_exit, index=df.index).fillna(False).astype(bool)
    if benchmark_entry_signal is None:
        # Legacy non-execution-aware callers already have an executable target stream.
        # A precomputed final-position stream must supply its independent requested path.
        benchmark_signal = None if positions_are_final else position.copy()
    else:
        if not isinstance(benchmark_entry_signal, pd.Series) or not benchmark_entry_signal.index.equals(df.index):
            return {"status": "INVALID_DATA", "reason": "benchmark_entry_signal_index_mismatch", "folds": []}
        benchmark_signal = benchmark_entry_signal.astype(float).replace([np.inf, -np.inf], np.nan)
        if benchmark_signal.isna().any():
            return {"status": "INVALID_DATA", "reason": "benchmark_entry_signal_nonfinite", "folds": []}
    benchmark_atr = None
    if benchmark_atr_for_entry is not None:
        if not isinstance(benchmark_atr_for_entry, pd.Series) or not benchmark_atr_for_entry.index.equals(df.index):
            return {"status": "INVALID_DATA", "reason": "benchmark_atr_index_mismatch", "folds": []}
        benchmark_atr = benchmark_atr_for_entry.astype(float)
    gross = position * ret
    turnover = execution_turnover(position, exit_flags)
    net = costed_execution_returns(
        position, ret, transaction_cost_bps=total_cost_bps,
        carry_bps_per_bar=carry, exit_mask=exit_flags,
    ).replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(lower=-1.0)
    fixed_cost = position.abs() * (carry / 10_000.0) if carry is not None else None

    fold_rows = []
    oos_returns = []
    oos_gross_returns = []
    oos_turnover = []
    oos_fixed_costs = []
    oos_ohlc_blocks: list[pd.DataFrame] = []
    oos_benchmark_entry_signal_blocks: list[np.ndarray] = []
    oos_benchmark_atr_blocks: list[np.ndarray] = []
    oos_candidate_net_blocks = []
    trades = 0
    for fold in splits:
        start, end = fold.test_start, fold.test_end
        x = net.iloc[start:end]
        p = position.iloc[start:end]
        oos_returns.append(x)
        oos_gross_returns.append(gross.iloc[start:end])
        oos_turnover.append(turnover.iloc[start:end])
        oos_ohlc_blocks.append(df.iloc[start:end].copy())
        if benchmark_signal is not None:
            oos_benchmark_entry_signal_blocks.append(benchmark_signal.iloc[start:end].to_numpy(dtype=float))
        if benchmark_atr is not None:
            oos_benchmark_atr_blocks.append(benchmark_atr.iloc[start:end].to_numpy(dtype=float))
        oos_candidate_net_blocks.append(x.to_numpy(dtype=float))
        if fixed_cost is not None:
            oos_fixed_costs.append(fixed_cost.iloc[start:end])
        prior_p = p.shift(1).fillna(0.0)
        trade_starts = (p != 0.0) & ((prior_p == 0.0) | (np.sign(p) != np.sign(prior_p)))
        trades += int(trade_starts.sum())
        x_values = x.to_numpy(dtype=float)
        fold_rows.append({
            "test_start": df.index[start].isoformat(),
            "test_end": df.index[end - 1].isoformat(),
            "bars": int(len(x)),
            "total_return": float(np.prod(1.0 + x_values) - 1.0) if len(x_values) else 0.0,
            "mean_return": float(np.mean(x_values)) if len(x_values) else 0.0,
        })

    all_oos = pd.concat(oos_returns) if oos_returns else pd.Series(dtype=float)
    values = all_oos.to_numpy(dtype=float)
    if len(values) == 0:
        return {"status": "INSUFFICIENT_DATA", "folds": fold_rows, "oos_bars": 0}
    mean_return = float(np.mean(values))
    std_return = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
    sharpe = (
        float(mean_return / std_return * math.sqrt(annualization))
        if std_return > 0 and math.isfinite(std_return)
        else None
    )
    equity = np.cumprod(1.0 + values)
    peak = np.maximum.accumulate(np.maximum(equity, 1.0))
    max_drawdown = float(np.min(equity / peak - 1.0)) if len(equity) else None
    all_oos_gross = pd.concat(oos_gross_returns) if oos_gross_returns else pd.Series(dtype=float)
    all_oos_turnover = pd.concat(oos_turnover) if oos_turnover else pd.Series(dtype=float)
    all_fixed_cost = pd.concat(oos_fixed_costs) if oos_fixed_costs else None
    cost_analysis = cost_breakeven_report(
        all_oos_gross.to_numpy(dtype=float),
        all_oos_turnover.to_numpy(dtype=float),
        taker_bps=effective_taker_bps,
        slippage_bps=effective_slippage_bps,
        bars_per_year=max(1, int(round(annualization))),
        stress_multiplier=stress_multiplier,
        fixed_cost_drag=None if all_fixed_cost is None else all_fixed_cost.to_numpy(dtype=float),
    )
    evidence_source = str(carry_evidence_source or "").strip()
    evidence_id = str(carry_evidence_id or "").strip()
    carry_configured = carry is not None
    cost_complete = carry_configured and bool(evidence_source) and bool(evidence_id)
    # A supplied OHLC-derived exit mask is not tick-level event-order evidence.
    # Promotion stays blocked until a validated tick-level stream is integrated.
    execution_complete = False
    cost_analysis["carry_rate_configured"] = carry_configured
    cost_analysis["carry_evidence_source_present"] = bool(evidence_source)
    cost_analysis["carry_evidence_id_present"] = bool(evidence_id)
    cost_analysis["financing_cost_model_complete"] = cost_complete
    cost_analysis["intrabar_stop_target_model_complete"] = execution_complete
    # The configured fee/slippage stress is not proof of venue-calibrated execution costs.
    cost_analysis["execution_cost_model_complete"] = False
    cost_analysis["execution_cost_calibration_status"] = "MISSING_VENUE_CALIBRATION_EVIDENCE"
    cost_analysis["execution_cost_calibration_evidence_id"] = None
    if not cost_complete:
        cost_analysis["note"] += (
            " Financing promotion evidence requires a configured per-bar carry value plus "
            "a traceable RESEARCH_CARRY_EVIDENCE_SOURCE and RESEARCH_CARRY_EVIDENCE_ID."
        )

    inference = newey_west_mean_inference(values)
    if benchmark_signal is None:
        benchmark_comparison = {
            "status": "INSUFFICIENT_DATA",
            "reason": "independent_entry_signal_missing",
        }
    else:
        benchmark_comparison = oos_benchmark_comparison(
            oos_ohlc_blocks,
            oos_benchmark_entry_signal_blocks,
            oos_candidate_net_blocks,
            oos_benchmark_atr_blocks if benchmark_atr is not None else None,
            transaction_cost_bps=total_cost_bps,
            carry_bps_per_bar=carry,
            stop_atr=benchmark_stop_atr,
            take_profit_atr=benchmark_take_profit_atr,
            drawdown_limit=benchmark_drawdown_limit,
            daily_loss_limit=benchmark_daily_loss_limit,
            loss_event_threshold=benchmark_loss_event_threshold,
            cooldown_bars=benchmark_cooldown_bars,
        )
    return {
        "status": "OK",
        "folds": fold_rows,
        "oos_bars": int(len(values)),
        "inference": inference,
        "benchmark_comparison": benchmark_comparison,
        "purge_bars": purge_bars,
        "embargo_bars": effective_embargo,
        "oos_total_return": float(np.prod(1.0 + values) - 1.0),
        "oos_mean_return": mean_return,
        "oos_return_sample": values[-5000:].tolist(),
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "trades": trades,
        "annualization_factor": annualization,
        "costs_bps": total_cost_bps,
        "cost_stress_ok": cost_analysis.get("stress_ok") is True and cost_complete,
        "cost_analysis": cost_analysis,
        "financing_cost_model_complete": cost_complete,
        "carry_rate_configured": carry_configured,
        "carry_evidence_source": evidence_source or None,
        "carry_evidence_id": evidence_id or None,
        "intrabar_stop_target_model_complete": execution_complete,
        # Carry evidence and OHLC exit accounting do not establish venue-calibrated execution costs.
        "execution_cost_model_complete": False,
        "execution_cost_calibration_status": "MISSING_VENUE_CALIBRATION_EVIDENCE",
        "execution_cost_calibration_evidence_id": None,
        "carry_bps_per_bar": carry,
    }


def regime_conditioned_performance(df: pd.DataFrame, returns: pd.Series, asset: str = "crypto") -> dict:
    close = df["close"].astype(float)
    fast = close.ewm(span=50, adjust=False).mean()
    slow = close.ewm(span=200, adjust=False).mean()
    # Use the observed bar cadence, matching the strategy feature/backtest engine.
    # Daily constants materially understate annualized volatility for intraday bars.
    annual_factor = _annualization_factor(df.index, asset=asset)
    vol = close.pct_change().rolling(20).std() * np.sqrt(annual_factor)
    regimes = pd.Series("RANGE", index=df.index)
    regimes = regimes.mask((fast > slow) & (vol < 0.60), "TREND_UP")
    regimes = regimes.mask((fast < slow) & (vol < 0.60), "TREND_DOWN")
    regimes = regimes.mask(vol >= 0.60, "HIGH_VOL")
    out = {}
    r = pd.Series(returns, index=df.index).fillna(0.0)
    for name in sorted(regimes.dropna().unique()):
        x = r[regimes == name]
        out[str(name)] = {"bars": int(len(x)), "mean_return": float(x.mean()) if len(x) else 0.0,
                          "total_return": float(np.prod(1 + x.to_numpy()) - 1) if len(x) else 0.0,
                          "annualization_factor": annual_factor}
    return out


def oos_promotion_gate(
    oos: dict,
    *,
    min_dsr: float | None = 0.95,
    require_cost_stress: bool = True,
    min_folds: int = 5,
    min_total_return: float = 0.0,
    min_positive_fold_ratio: float = 0.5,
    max_negative_fold_return: float = -0.20,
    require_last_fold_positive: bool = True,
    min_sharpe: float | None = None,
    max_drawdown: float | None = -0.20,
    min_trades: int | None = 100,
    min_mean_t_statistic: float | None = None,
    min_effective_observations: int | None = None,
    require_benchmark_excess_return: bool = False,
) -> dict:
    """Fail-closed OOS promotion gate with explicit rejection reasons."""
    def finite_number(value):
        if value is None or isinstance(value, (bool, np.bool_)):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if math.isfinite(parsed) else None

    raw_folds = oos.get("folds")
    fold_list_valid = isinstance(raw_folds, (list, tuple))
    folds = list(raw_folds) if fold_list_valid else []
    returns = []
    for fold in folds:
        if not isinstance(fold, dict):
            fold_list_valid = False
            continue
        value = finite_number(fold.get("total_return"))
        if value is None:
            fold_list_valid = False
            continue
        returns.append(value)
    fold_metrics_complete = fold_list_valid and len(returns) == len(folds)
    positive_ratio = (sum(value > 0 for value in returns) / len(returns)) if returns else 0.0
    worst = min(returns) if returns else -1.0
    last_positive = bool(returns and returns[-1] > 0)

    raw_oos_return = oos.get("oos_total_return") if "oos_total_return" in oos else oos.get("total_return")
    oos_total_return = finite_number(raw_oos_return)
    sharpe = finite_number(oos.get("sharpe"))
    max_dd = finite_number(oos.get("max_drawdown"))
    raw_trades = finite_number(oos.get("trades"))
    trades = (
        int(raw_trades)
        if raw_trades is not None and raw_trades >= 0 and raw_trades.is_integer()
        else None
    )

    sharpe_ok = sharpe is not None and (min_sharpe is None or sharpe >= float(min_sharpe))
    dd_ok = True if max_drawdown is None else (max_dd is not None and max_dd >= float(max_drawdown))
    trades_ok = True if min_trades is None else (trades is not None and trades >= int(min_trades))
    dsr_value = finite_number(oos.get("deflated_sharpe"))
    # Missing DSR cannot meet a configured floor; no synthetic value is substituted.
    dsr_ok = True if min_dsr is None else (dsr_value is not None and dsr_value >= float(min_dsr))
    stress_value = oos.get("cost_stress_ok")
    stress_ok = True if not require_cost_stress else stress_value is True
    # These three fields are an explicit promotion contract, not optional hints from
    # upstream callers. Without a traceable carry schedule and intrabar stop/target
    # model, a plausible Sharpe or manually supplied cost_stress_ok=True is insufficient.
    financing_cost_model_complete = oos.get("financing_cost_model_complete") is True
    intrabar_stop_target_model_complete = oos.get("intrabar_stop_target_model_complete") is True
    execution_cost_calibration_status = str(oos.get("execution_cost_calibration_status") or "").strip().upper()
    execution_cost_calibration_evidence_id = str(oos.get("execution_cost_calibration_evidence_id") or "").strip()
    execution_cost_model_complete = oos.get("execution_cost_model_complete") is True
    universe_bias = str(oos.get("universe_bias") or "")
    survivorship_bias_blocked = universe_bias == "CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY"
    slippage_model_complete = oos.get("slippage_model_complete") is True
    execution_cost_calibration_ok = (
        execution_cost_calibration_status == "VERIFIED"
        and bool(execution_cost_calibration_evidence_id)
    )
    trial_registry_complete = oos.get("trial_registry_complete") is True
    status_ok = str(oos.get("status", "OK")) == "OK"
    inference = oos.get("inference") if isinstance(oos.get("inference"), dict) else {}
    mean_t_stat = finite_number(inference.get("newey_west_t_statistic"))
    effective_observations = finite_number(inference.get("effective_observations"))
    inference_ok = (
        True if min_mean_t_statistic is None and min_effective_observations is None
        else inference.get("status") == "OK"
        and mean_t_stat is not None
        and effective_observations is not None
        and (min_mean_t_statistic is None or mean_t_stat >= float(min_mean_t_statistic))
        and (min_effective_observations is None or effective_observations >= int(min_effective_observations))
    )
    benchmark = oos.get("benchmark_comparison") if isinstance(oos.get("benchmark_comparison"), dict) else {}
    benchmark_ok = True if not require_benchmark_excess_return else (
        benchmark.get("status") == "OK" and benchmark.get("gate_passed") is True
    )
    enough_folds = len(returns) >= int(min_folds)
    total_return_ok = oos_total_return is not None and oos_total_return >= float(min_total_return)
    positive_ratio_ok = positive_ratio >= float(min_positive_fold_ratio)
    worst_fold_ok = bool(returns) and worst >= float(max_negative_fold_return)
    last_fold_ok = (last_positive if require_last_fold_positive else True)

    rejection_reasons = []
    if not status_ok:
        rejection_reasons.append("oos_status_not_ok")
    if not fold_metrics_complete:
        rejection_reasons.append("fold_metrics_missing_or_invalid")
    if not enough_folds:
        rejection_reasons.append("insufficient_oos_folds")
    if not total_return_ok:
        rejection_reasons.append("missing_or_below_minimum_oos_total_return")
    if not positive_ratio_ok:
        rejection_reasons.append("positive_fold_ratio_below_minimum")
    if not worst_fold_ok:
        rejection_reasons.append("worst_fold_return_below_minimum_or_missing")
    if require_last_fold_positive and not last_fold_ok:
        rejection_reasons.append("last_oos_fold_not_positive")
    if not sharpe_ok:
        rejection_reasons.append("missing_or_below_minimum_oos_sharpe")
    if not inference_ok:
        rejection_reasons.append("missing_or_insignificant_newey_west_mean_return")
    if not benchmark_ok:
        rejection_reasons.append("benchmark_excess_return_not_positive")
    if not dd_ok:
        rejection_reasons.append("missing_or_excessive_oos_drawdown")
    if not trades_ok:
        rejection_reasons.append("missing_or_insufficient_oos_trades")
    if not dsr_ok:
        rejection_reasons.append("missing_or_below_minimum_deflated_sharpe")
    if not stress_ok:
        rejection_reasons.append("cost_stress_not_passed")
    if not financing_cost_model_complete:
        rejection_reasons.append("financing_cost_model_incomplete")
    if not intrabar_stop_target_model_complete:
        rejection_reasons.append("intrabar_stop_target_model_incomplete")
    if not execution_cost_calibration_ok:
        rejection_reasons.append("venue_execution_cost_calibration_missing")
    if not execution_cost_model_complete:
        rejection_reasons.append("execution_cost_model_incomplete")
    if not trial_registry_complete:
        rejection_reasons.append("comparable_trial_registry_incomplete")
    if survivorship_bias_blocked:
        rejection_reasons.append("crypto_universe_survivorship_bias_not_resolved")
    if not slippage_model_complete:
        rejection_reasons.append("volume_volatility_aware_slippage_model_incomplete")

    passed = not rejection_reasons
    return {
        "passed": passed,
        "rejection_reasons": rejection_reasons,
        "folds": len(returns),
        "fold_metrics_complete": fold_metrics_complete,
        "positive_fold_ratio": positive_ratio,
        "worst_fold_return": worst,
        "last_fold_positive": last_positive,
        "oos_total_return": oos_total_return,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "trades": trades,
        "sharpe_ok": sharpe_ok,
        "max_drawdown_ok": dd_ok,
        "min_trades_ok": trades_ok,
        "thresholds": {
            "min_folds": min_folds,
            "min_total_return": min_total_return,
            "min_positive_fold_ratio": min_positive_fold_ratio,
            "max_negative_fold_return": max_negative_fold_return,
            "require_last_fold_positive": require_last_fold_positive,
            "min_sharpe": min_sharpe,
            "max_drawdown": max_drawdown,
            "min_trades": min_trades,
            "trial_registry_complete_required": True,
            "min_mean_t_statistic": min_mean_t_statistic,
            "min_effective_observations": min_effective_observations,
            "require_benchmark_excess_return": require_benchmark_excess_return,
        },
        "inference": inference,
        "benchmark_comparison": benchmark or None,
        "deflated_sharpe": dsr_value,
        "min_dsr": min_dsr,
        "dsr_ok": dsr_ok,
        "cost_stress_ok": stress_value,
        "require_cost_stress": require_cost_stress,
        "stress_ok": stress_ok,
        "financing_cost_model_complete": financing_cost_model_complete,
        "intrabar_stop_target_model_complete": intrabar_stop_target_model_complete,
        "execution_cost_model_complete": execution_cost_model_complete,
        "execution_cost_calibration_status": execution_cost_calibration_status or "MISSING_VENUE_CALIBRATION_EVIDENCE",
        "execution_cost_calibration_evidence_present": bool(execution_cost_calibration_evidence_id),
        "execution_cost_calibration_ok": execution_cost_calibration_ok,
        "trial_registry_complete": trial_registry_complete,
        "trial_registry_ok": trial_registry_complete,
        "universe_bias": universe_bias or None,
        "survivorship_bias_blocked": survivorship_bias_blocked,
        "slippage_model_complete": slippage_model_complete,
    }


def monte_carlo_bootstrap(returns, *, trials: int = 2000, seed: int = 7, block_size: int = 5) -> dict:
    """Time-series-aware block bootstrap stress summary; research only, not a forecast."""
    arr=np.asarray(list(returns),dtype=float)
    arr=arr[np.isfinite(arr)]
    if len(arr)<20: return {"status":"INSUFFICIENT_DATA","observations":int(len(arr))}
    rng=np.random.default_rng(seed)
    n=len(arr); size=max(1,min(int(block_size),n))
    blocks=[arr[i:i+size] for i in range(0,n-size+1)]
    outcomes=np.empty(trials); max_dd=np.empty(trials)
    for i in range(trials):
        sample=[]
        while len(sample)<n:
            sample.extend(blocks[int(rng.integers(0,len(blocks)))].tolist())
        sample=np.asarray(sample[:n],dtype=float)
        equity=np.cumprod(1.0+sample)
        peak=np.maximum.accumulate(equity)
        dd=equity/peak-1.0
        outcomes[i]=equity[-1]-1.0; max_dd[i]=dd.min()
    return {"status":"OK","observations":int(n),"trials":int(trials),"block_size":int(size),"return_p05":float(np.quantile(outcomes,0.05)),"return_median":float(np.quantile(outcomes,0.50)),"return_p95":float(np.quantile(outcomes,0.95)),"drawdown_p05":float(np.quantile(max_dd,0.05)),"drawdown_median":float(np.quantile(max_dd,0.50)),"probability_negative_return":float(np.mean(outcomes<0))}

def parameter_plateau_score(results: dict, *, metric: str = "sharpe", tolerance: float = 0.20) -> dict:
    """Measure whether nearby parameter variants retain most of the best metric."""
    rows=results.get("variants") if isinstance(results,dict) else None
    if not rows: return {"status":"INSUFFICIENT_DATA"}
    vals=[float(r.get(metric,0.0)) for r in rows if np.isfinite(float(r.get(metric,0.0)))]
    if not vals: return {"status":"INSUFFICIENT_DATA"}
    best=max(vals); floor=best*(1.0-float(tolerance))
    plateau=sum(v>=floor for v in vals)/len(vals)
    return {"status":"OK","metric":metric,"best":best,"floor":floor,"plateau_ratio":plateau,"robust":plateau>=0.30,"variants":len(vals)}


def deflated_sharpe_report(net_returns, trial_sharpes_annual, n_trials: int, bars_per_year: int) -> dict:
    """Formal Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014) for the selected strategy's net per-bar returns.

    n_trials must count EVERY variant evaluated on this data (strategies, parameter variants, AI configs, earlier runs);
    the dispersion of trial Sharpes is estimated from the ones supplied, so with few trials treat the number as optimistic.
    """
    from .research_stats import deflated_sharpe_ratio, expected_max_sharpe
    x = np.asarray([v for v in np.asarray(list(net_returns), dtype=float) if np.isfinite(v)], dtype=float)
    if len(x) < 30 or float(x.std(ddof=1)) <= 0:
        return {"status": "INSUFFICIENT_DATA", "observations": int(len(x))}
    sr = float(x.mean() / x.std(ddof=1))
    centred = (x - x.mean()) / x.std(ddof=1)
    skew = float(np.mean(centred ** 3))
    kurt = float(np.mean(centred ** 4))
    per_period = [float(s) / math.sqrt(max(1, bars_per_year)) for s in trial_sharpes_annual if np.isfinite(s)]
    var = float(np.var(per_period, ddof=1)) if len(per_period) > 1 else 0.0
    trials = max(1, int(n_trials))
    dsr = deflated_sharpe_ratio(sr, len(x), trials, var, skew=skew, kurt=kurt)
    return {"status": "OK", "deflated_sharpe_ratio": float(dsr), "per_period_sharpe": sr,
            "sharpe_hurdle_per_period": float(expected_max_sharpe(trials, var)), "trials": trials,
            "observations": int(len(x)), "skew": skew, "kurtosis": kurt,
            "note": "Probability the Sharpe is real after correcting for selection among `trials`; >= 0.95 is the usual bar."}


def cost_breakeven_report(
    returns_gross,
    turnover,
    *,
    taker_bps: float,
    slippage_bps: float,
    bars_per_year: int,
    stress_multiplier: float = 2.0,
    fixed_cost_drag: Iterable[float] | None = None,
) -> dict:
    """Stress trading fees/slippage plus any fixed per-bar drag such as carry/funding.

    `fixed_cost_drag` is non-negative per-bar cost already measured against the same
    return/turnover observations. The break-even variable fee estimate deducts that
    fixed drag first. Missing observations or non-finite/negative cost inputs fail closed.
    """
    g = np.asarray(list(returns_gross), dtype=float)
    t = np.asarray(list(turnover), dtype=float)
    fixed = np.zeros(len(g), dtype=float) if fixed_cost_drag is None else np.asarray(list(fixed_cost_drag), dtype=float)
    n = min(len(g), len(t), len(fixed))
    if n < 30:
        return {"status": "INSUFFICIENT_DATA", "observations": int(n), "stress_ok": False}
    g, t, fixed = g[:n], t[:n], fixed[:n]
    try:
        taker = float(taker_bps)
        slippage = float(slippage_bps)
        multiplier = float(stress_multiplier)
    except (TypeError, ValueError, OverflowError):
        return {"status": "INVALID_COST_INPUT", "observations": int(n), "stress_ok": False}
    if (
        not np.all(np.isfinite(g))
        or not np.all(np.isfinite(t))
        or not np.all(np.isfinite(fixed))
        or np.any(t < 0)
        or np.any(fixed < 0)
        or not np.isfinite(taker)
        or not np.isfinite(slippage)
        or not np.isfinite(multiplier)
        or taker < 0
        or slippage < 0
        or multiplier <= 0
    ):
        return {"status": "INVALID_COST_INPUT", "observations": int(n), "stress_ok": False}
    variable_cost = (taker + slippage) / 10_000.0
    mean_gross = float(g.mean())
    mean_fixed = float(fixed.mean())
    mean_turn = float(t.mean())
    net = float((g - fixed - t * variable_cost).mean())
    stressed = float((g - fixed - t * variable_cost * multiplier).mean())
    breakeven_bps = ((mean_gross - mean_fixed) / mean_turn * 10_000.0) if mean_turn > 0 else float("inf")
    per_year = max(1, int(bars_per_year))
    return {
        "status": "OK",
        "observations": int(n),
        "annual_turnover": mean_turn * per_year,
        "annual_gross_return": mean_gross * per_year,
        "annual_fixed_cost_drag": mean_fixed * per_year,
        "annual_variable_cost_drag": mean_turn * variable_cost * per_year,
        "annual_cost_drag": (mean_fixed + mean_turn * variable_cost) * per_year,
        "annual_net_return": net * per_year,
        "annual_net_return_stressed": stressed * per_year,
        "assumed_cost_bps": variable_cost * 10_000.0,
        "breakeven_cost_bps": breakeven_bps,
        "cost_headroom": (breakeven_bps / (variable_cost * 10_000.0)) if variable_cost > 0 and np.isfinite(breakeven_bps) else None,
        "stress_multiplier": multiplier,
        "stress_ok": bool(stressed > 0.0 and net > 0.0),
        "note": "Arithmetic annualisation of mean bar returns; carry/funding is included in fixed_cost_drag when supplied. The stress gate multiplies trading fees/slippage and retains the full fixed cost.",
    }


def edge_decay_check(realized_returns, *, expected_mean: float, min_obs: int = 60, z_halt: float = 2.0,
                     z_warn: float = 1.0) -> dict:
    """Is live/paper performance statistically worse than the research expectation?

    z = (realized_mean - expected_mean) / standard_error. A strongly negative z means the edge that justified running the bot
    is no longer visible. Returns status OK | WARN | HALT_NEW_ENTRIES | INSUFFICIENT_DATA. Pure function: the caller decides what
    to do (the intended action is stop NEW entries and open an incident, never auto-resume).
    """
    x = np.asarray([v for v in np.asarray(list(realized_returns), dtype=float) if np.isfinite(v)], dtype=float)
    if len(x) < int(min_obs):
        return {"status": "INSUFFICIENT_DATA", "observations": int(len(x)), "min_obs": int(min_obs)}
    sd = float(x.std(ddof=1))
    if sd <= 0:
        return {"status": "INSUFFICIENT_DATA", "observations": int(len(x)), "reason": "zero variance"}
    se = sd / math.sqrt(len(x))
    z = (float(x.mean()) - float(expected_mean)) / se
    status = "HALT_NEW_ENTRIES" if z <= -abs(z_halt) else ("WARN" if z <= -abs(z_warn) else "OK")
    return {"status": status, "z": float(z), "realized_mean": float(x.mean()), "expected_mean": float(expected_mean),
            "observations": int(len(x)), "standard_error": float(se)}

