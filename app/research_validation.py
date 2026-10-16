from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable
import math
import numpy as np
import pandas as pd
from .strategy_engine import next_open_returns, _annualization_factor
from .execution_aware_research import costed_execution_returns, execution_turnover


@dataclass(frozen=True)
class WalkForwardFold:
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    purge: int
    embargo: int


def validate_ohlcv_frame(df: pd.DataFrame, require_volume: bool = False) -> dict:
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
    return {"valid": not issues, "issues": issues, "rows": int(len(df))}


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


def multiple_testing_sharpe_adjustment(sharpes: Iterable[float], trials: int) -> dict:
    s = np.asarray([x for x in sharpes if np.isfinite(x)], dtype=float)
    if len(s) == 0:
        return {"status": "INSUFFICIENT_DATA"}
    # Conservative benchmark: expected maximum of trials of N(0,1), scaled to the
    # observed Sharpe dispersion. This is a screening proxy, not a formal DSR.
    m = max(1, int(trials))
    expected_max = math.sqrt(2.0 * math.log(m)) if m > 1 else 0.0
    best = float(np.max(s))
    dispersion = float(np.std(s)) if len(s) > 1 else 1.0
    adjusted = best - expected_max * dispersion
    return {"status": "OK", "best_sharpe": best, "trials": m,
            "expected_null_max_proxy": expected_max, "adjusted_sharpe_proxy": float(adjusted),
            "note": "Screening proxy only; not a formal Deflated Sharpe Ratio."}


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
) -> dict:
    """Evaluate held-out returns with the same executable position/stop/carry stream.

    With bar_returns and intrabar_exit supplied, the stream comes from the OHLC execution
    simulator and positions_are_final must be true. Without them the function remains a
    legacy next-open diagnostic and reports intrabar parity as incomplete. Missing carry
    is reported as incomplete evidence rather than silently certified as zero financing.
    """
    splits = purged_walk_forward_splits(len(df), folds=folds, min_train=min_train, purge=1, embargo=1)
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
    trades = 0
    for fold in splits:
        start, end = fold.test_start, fold.test_end
        x = net.iloc[start:end]
        p = position.iloc[start:end]
        oos_returns.append(x)
        oos_gross_returns.append(gross.iloc[start:end])
        oos_turnover.append(turnover.iloc[start:end])
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

    return {
        "status": "OK",
        "folds": fold_rows,
        "oos_bars": int(len(values)),
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
    min_sharpe: float | None = 0.5,
    max_drawdown: float | None = -0.20,
    min_trades: int | None = 100,
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

    sharpe_ok = True if min_sharpe is None else (sharpe is not None and sharpe >= float(min_sharpe))
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
    execution_cost_calibration_ok = (
        execution_cost_calibration_status == "VERIFIED"
        and bool(execution_cost_calibration_evidence_id)
    )
    trial_registry_complete = oos.get("trial_registry_complete") is True
    universe_bias = str(oos.get("universe_bias") or "")
    survivorship_bias_blocked = universe_bias == "CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY"
    # Missing calibration status is not evidence of a complete execution-cost model.
    # Require an explicit True instead of allowing callers that omit the new field
    # to bypass the fixed-slippage promotion block.
    slippage_model_complete = oos.get("slippage_model_complete") is True
    status_ok = str(oos.get("status", "OK")) == "OK"
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
        },
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
