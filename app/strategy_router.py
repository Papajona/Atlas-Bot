from __future__ import annotations

"""Regime-aware strategy selection for the adaptive trading bot.

The router is deliberately subordinate to the deterministic risk engine. It only
answers three questions:
  1. What regime is the completed bar in?
  2. Which validated strategy family has the strongest evidence for that regime?
  3. Should the current strategy be held, switched, blended, or abstained?

Selection uses only data at or before the decision bar. Live execution still has
to pass execute_signal()/risk_gate().
"""

from datetime import datetime, timezone
from collections.abc import Mapping
import math
import numpy as np
import pandas as pd

from .strategy_engine import StrategyConfig, strategy_signals, next_open_returns, _annualization_factor, target_positions
from .research_validation import purged_walk_forward_splits
from .strategy_selection import paired_return_lead
from .execution_costs import EXECUTION_COST_MODEL_VERSION, net_returns_after_costs
from .research_execution_costs import (
    execution_cost_for_event,
    execution_cost_series_for_positions,
    is_verified_research_execution_cost_context,
    slice_research_execution_context,
)

STRATEGIES = ("trend", "momentum", "breakout", "mean_reversion", "ensemble")
REGIMES = ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL", "TRANSITION")


def _execution_cost_context_audit(
    context: dict[str, object] | None,
) -> dict[str, object]:
    """Expose the verified venue calibration identity with routed research returns."""
    values = context if isinstance(context, Mapping) else {}
    evidence_id = str(
        values.get("execution_cost_calibration_evidence_id")
        or values.get("evidence_id")
        or ""
    ).strip()
    claimed_status = str(
        values.get("execution_cost_calibration_status") or ""
    ).strip().upper()
    trusted_context = is_verified_research_execution_cost_context(context)
    calibration_verified = (
        trusted_context and claimed_status == "VERIFIED" and bool(evidence_id)
    )
    if calibration_verified:
        calibration_status = "VERIFIED"
    elif claimed_status == "VERIFIED":
        calibration_status = "UNATTESTED_EXECUTION_COST_CONTEXT"
    else:
        calibration_status = claimed_status or (
            "UNATTESTED_EXECUTION_COST_CONTEXT"
            if values else "MISSING_VENUE_CALIBRATION_EVIDENCE"
        )
    return {
        "execution_cost_calibration_status": calibration_status,
        "execution_cost_calibration_verified": calibration_verified,
        "execution_cost_calibration_evidence_id": evidence_id or None,
        "execution_cost_model_fingerprint": values.get("model_fingerprint"),
        "per_bar_execution_context_sha256": values.get("context_sha256"),
        "execution_cost_venue": values.get("venue"),
        "execution_cost_market_type": values.get("market_type"),
        "execution_cost_symbol": values.get("symbol"),
        "execution_cost_timeframe": values.get("timeframe"),
        "execution_cost_dataset_sha256": values.get("dataset_sha256"),
    }


def classify_regime(df: pd.DataFrame, *, asset: str = "crypto") -> pd.Series:
    """Classify each completed bar without using future observations."""
    c = df["close"].astype(float)
    fast = c.ewm(span=50, adjust=False).mean()
    slow = c.ewm(span=200, adjust=False).mean()
    # Annualize using the observed cadence; fixed daily constants understate
    # intraday volatility and can suppress the HIGH_VOL regime on hourly bars.
    annual = _annualization_factor(df.index, asset=asset)
    vol = c.pct_change().rolling(20).std() * np.sqrt(annual)
    trend_gap = (fast / slow - 1.0).abs()
    trend = (fast / slow - 1.0)
    regime = pd.Series("RANGE", index=df.index, dtype="object")
    regime = regime.mask(vol >= 0.60, "HIGH_VOL")
    stable_trend = (vol < 0.60) & (trend_gap >= 0.004)
    regime = regime.mask(stable_trend & (trend > 0), "TREND_UP")
    regime = regime.mask(stable_trend & (trend < 0), "TREND_DOWN")
    # A narrow gap with changing trend direction is explicitly treated as a transition
    # rather than pretending the market is cleanly trending or ranging.
    direction = np.sign(trend).replace(0, np.nan).ffill().fillna(0)
    direction_change = direction.diff().abs().fillna(0) > 1
    regime = regime.mask(direction_change & (vol < 0.60), "TRANSITION")
    regime.attrs["annualization_factor"] = annual
    return regime


def _strategy_returns(
    df: pd.DataFrame,
    cfg: StrategyConfig,
    strategy: str,
    costs_bps: float,
    asset: str,
    *,
    execution_cost_context: Mapping[str, object] | None = None,
    execution_cost_namespace: str = "strategy-router",
    precomputed_execution_costs: dict[str, tuple[pd.Series, dict[str, object]]] | None = None,
) -> pd.Series:
    """Return the executable strategy stream with evidence-backed event costs when available."""
    signal_frame = strategy_signals(df, cfg, asset=asset)
    # A completed-bar signal becomes an executable position at the next bar open.
    position = target_positions(
        signal_frame[strategy], signal_frame["realized_vol"], cfg
    ).shift(1).fillna(0.0)
    market_returns = next_open_returns(df) - 1.0
    turnover = position.diff().abs().fillna(position.abs())
    reference_prices = df["open"].astype(float)
    cached_costs = (
        precomputed_execution_costs.get(strategy)
        if isinstance(precomputed_execution_costs, dict)
        else None
    )
    sliced_cached = (
        _slice_precomputed_execution_costs(cached_costs, position.index)
        if cached_costs is not None else None
    )
    if sliced_cached is not None:
        per_bar_costs, cost_audit = sliced_cached
    elif execution_cost_context is None:
        # Preserve the caller's configured scalar cost without an O(history)
        # Python event loop on every adaptive-router decision.
        per_bar_costs = pd.Series(
            float(costs_bps), index=position.index, dtype=float,
            name="execution_cost_bps",
        )
        cost_audit = {
            "execution_cost_model_complete": False,
            "execution_cost_model_status": "CALLER_STATIC_COST_NO_CALIBRATED_CONTEXT",
            "execution_cost_model_fingerprint": None,
            "per_bar_execution_context_sha256": None,
            "execution_cost_failure_reasons": {
                "EXECUTION_COST_CONTEXT_NOT_EXPLICITLY_SUPPLIED": 1
            },
        }
    else:
        per_bar_costs, cost_audit = execution_cost_series_for_positions(
            execution_cost_context,
            position,
            reference_prices,
            namespace=f"{execution_cost_namespace}:{strategy}",
            fallback_cost_bps=costs_bps,
        )
    net_values = net_returns_after_costs(
        position.to_numpy(dtype=float) * market_returns.to_numpy(dtype=float),
        turnover=turnover.to_numpy(dtype=float),
        execution_cost_bps=per_bar_costs.to_numpy(dtype=float),
        exposure=position.to_numpy(dtype=float),
    )
    result = pd.Series(net_values, index=position.index, dtype=float, name="net_return")
    result.attrs.update(cost_audit)
    return result

def _slice_precomputed_execution_costs(
    cached: tuple[pd.Series, dict[str, object]], index: pd.Index
) -> tuple[pd.Series, dict[str, object]] | None:
    """Slice one precomputed causal cost stream and its audit to an exact prefix."""
    costs, full_audit = cached
    if not isinstance(costs, pd.Series):
        return None
    positions = costs.index.get_indexer(index)
    if len(positions) != len(index) or (positions < 0).any():
        return None
    sliced = pd.Series(
        costs.to_numpy(dtype=float)[positions],
        index=index,
        dtype=float,
        name=costs.name or "execution_cost_bps",
    )
    if not np.isfinite(sliced.to_numpy(dtype=float)).all():
        return None
    complete_by_bar = full_audit.get("_execution_cost_model_complete_by_bar")
    reasons_by_bar = full_audit.get("_execution_cost_failure_reasons_by_bar")
    if not isinstance(complete_by_bar, list) or len(complete_by_bar) != len(costs):
        return None
    if not isinstance(reasons_by_bar, list) or len(reasons_by_bar) != len(costs):
        return None
    complete = all(bool(complete_by_bar[position]) for position in positions)
    reasons: dict[str, int] = {}
    for position in positions:
        row_reasons = reasons_by_bar[position]
        if isinstance(row_reasons, dict):
            for reason, count in row_reasons.items():
                reasons[str(reason)] = reasons.get(str(reason), 0) + int(count)
    audit: dict[str, object] = {
        "execution_cost_model_complete": bool(complete),
        "execution_cost_model_status": (
            "VERIFIED_PER_BAR_EXECUTION_COSTS"
            if complete else "STATIC_FALLBACK_OR_INCOMPLETE_PER_BAR_CONTEXT"
        ),
        "execution_cost_model_fingerprint": full_audit.get(
            "execution_cost_model_fingerprint"
        ),
        "per_bar_execution_context_sha256": full_audit.get(
            "per_bar_execution_context_sha256"
        ),
        "execution_cost_failure_reasons": reasons,
    }
    return sliced, audit


def _precompute_router_execution_costs(
    df: pd.DataFrame,
    cfg: StrategyConfig,
    execution_cost_context: Mapping[str, object] | None,
    *,
    costs_bps: float,
    asset: str,
) -> dict[str, tuple[pd.Series, dict[str, object]]]:
    """Price every strategy's causal position stream once for walk-forward reuse."""
    if execution_cost_context is None:
        return {}
    signals = strategy_signals(df, cfg, asset=asset)
    reference_prices = df["open"].astype(float)
    precomputed: dict[str, tuple[pd.Series, dict[str, object]]] = {}
    for strategy in STRATEGIES:
        positions = target_positions(
            signals[strategy], signals["realized_vol"], cfg
        ).shift(1).fillna(0.0)
        precomputed[strategy] = execution_cost_series_for_positions(
            execution_cost_context,
            positions,
            reference_prices,
            namespace=f"strategy-router:{strategy}",
            fallback_cost_bps=costs_bps,
        )
    return precomputed


def _metrics(
    x: pd.Series, *, asset: str = "crypto", annualization_factor: float | None = None
) -> dict:
    x = pd.Series(x).replace([np.inf, -np.inf], np.nan).dropna()
    if x.empty:
        return {"bars": 0, "trades": 0, "total_return": 0.0, "sharpe": 0.0,
                "max_drawdown": 0.0, "mean_return": 0.0}
    eq = (1.0 + x).cumprod()
    peak = eq.cummax()
    dd = eq / peak - 1.0
    std = float(x.std())
    if annualization_factor is None:
        annualization_factor = _annualization_factor(x.index, asset=asset)
    else:
        annualization_factor = float(annualization_factor)
        if not math.isfinite(annualization_factor) or annualization_factor <= 0:
            raise ValueError("annualization_factor must be finite and positive")
    sharpe = float(x.mean() / std * math.sqrt(annualization_factor)) if std > 0 else 0.0
    return {
        "bars": int(len(x)),
        "trades": int(np.count_nonzero(np.diff(np.r_[0.0, np.sign(x.to_numpy())]))),
        "total_return": float(eq.iloc[-1] - 1.0),
        "sharpe": sharpe,
        "annualization_factor": annualization_factor,
        "max_drawdown": float(dd.min()),
        "mean_return": float(x.mean()),
    }


def regime_conditioned_strategy_performance(
    df: pd.DataFrame,
    cfg: StrategyConfig | None = None,
    *,
    costs_bps: float = 7.5,
    asset: str = "crypto",
    execution_cost_context: Mapping[str, object] | None = None,
    precomputed_execution_costs: dict[str, tuple[pd.Series, dict[str, object]]] | None = None,
) -> dict:
    """Compute per-strategy performance separately inside each market regime."""
    cfg = cfg or StrategyConfig()
    annualization_factor = _annualization_factor(df.index, asset=asset)
    regimes = classify_regime(df, asset=asset)
    result: dict[str, dict] = {}
    for strategy in STRATEGIES:
        returns = _strategy_returns(
            df, cfg, strategy, costs_bps, asset,
            execution_cost_context=execution_cost_context,
            precomputed_execution_costs=precomputed_execution_costs,
        )
        signal_frame = strategy_signals(df, cfg, asset=asset)
        positions = target_positions(
            signal_frame[strategy], signal_frame["realized_vol"], cfg
        ).shift(1).fillna(0.0)
        result[strategy] = {}
        for regime in sorted(set(regimes.dropna().astype(str))):
            mask = regimes == regime
            x = returns.loc[mask]
            metrics = _metrics(x, asset=asset, annualization_factor=annualization_factor)
            regime_positions = positions.loc[mask]
            metrics["trades"] = int(
                np.count_nonzero(np.diff(np.r_[0.0, regime_positions.to_numpy()]))
            )
            for audit_key in (
                "execution_cost_model_complete",
                "execution_cost_model_status",
                "execution_cost_model_fingerprint",
                "per_bar_execution_context_sha256",
                "execution_cost_failure_reasons",
            ):
                metrics[audit_key] = returns.attrs.get(audit_key)
            metrics.update(_execution_cost_context_audit(execution_cost_context))
            result[strategy][regime] = metrics
    return result

def _score_metrics(m: dict, *, min_trades: int = 20) -> float:
    if int(m.get("bars", 0)) < 30:
        return -10.0
    # Evidence is risk-adjusted and deliberately conservative. Drawdown is penalized
    # twice as a stability control; return alone can never dominate the score.
    trades = int(m.get("trades", 0))
    if trades < min_trades:
        return -5.0
    sharpe = float(m.get("sharpe", 0.0))
    ret = float(m.get("total_return", 0.0))
    dd = abs(float(m.get("max_drawdown", 0.0)))
    return sharpe + 0.75 * ret - 1.25 * dd


def _recent_regime_stability(regimes: pd.Series, bars: int = 3) -> tuple[str, int]:
    if regimes.empty:
        return "UNKNOWN", 0
    tail = [str(x) for x in regimes.tail(max(1, int(bars))).tolist()]
    current = tail[-1]
    stable = 0
    for x in reversed(tail):
        if x != current:
            break
        stable += 1
    return current, stable


def online_strategy_scores(
    outcomes,
    *,
    current_regime: str | None = None,
    timeframe: str | None = None,
    half_life_days: float = 14.0,
    min_observations: int = 10,
) -> dict[str, float]:
    """Bounded, regime-aware online experience scores.

    The learner prefers net return in the current regime/timeframe. If there are too
    few observations for that slice it shrinks toward the broader customer/symbol
    experience already supplied by the caller. Future information is never used by
    the live selector: only completed StrategyOutcome rows are accepted here.
    """
    now = datetime.now(timezone.utc)

    def collect(rows):
        grouped: dict[str, list[tuple[float, float]]] = {}
        for row in rows or []:
            strategy = row.get("strategy") if isinstance(row, dict) else getattr(row, "strategy", "")
            if strategy not in STRATEGIES:
                continue
            value = None
            for field in ("net_return_bps", "return_bps"):
                value = row.get(field) if isinstance(row, dict) else getattr(row, field, None)
                if value is not None:
                    break
            if value is None:
                continue
            try:
                value = float(value)
            except Exception:
                continue
            created = row.get("created_at") if isinstance(row, dict) else getattr(row, "created_at", None)
            if created is None:
                age_days = 0.0
            else:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_days = max(0.0, (now - created).total_seconds() / 86400.0)
            weight = math.pow(0.5, age_days / max(0.25, float(half_life_days)))
            grouped.setdefault(strategy, []).append((value, weight))
        out: dict[str, tuple[float, int]] = {}
        for name, vals in grouped.items():
            den = sum(w for _, w in vals)
            mean_bps = sum(v * w for v, w in vals) / den if den else 0.0
            out[name] = (float(mean_bps), len(vals))
        return out

    global_stats = collect(outcomes)
    regime_rows = []
    if current_regime:
        for row in outcomes or []:
            r = row.get("regime") if isinstance(row, dict) else getattr(row, "regime", None)
            tf = row.get("timeframe") if isinstance(row, dict) else getattr(row, "timeframe", None)
            if str(r or "") == str(current_regime):
                if timeframe and str(tf or timeframe) != str(timeframe):
                    continue
                regime_rows.append(row)
    regime_stats = collect(regime_rows)

    scores = {name: 0.0 for name in STRATEGIES}
    for name in STRATEGIES:
        g_mean, g_n = global_stats.get(name, (0.0, 0))
        r_mean, r_n = regime_stats.get(name, (0.0, 0))
        if r_n >= int(min_observations):
            mean_bps = 0.70 * r_mean + 0.30 * g_mean
        elif g_n >= int(min_observations):
            mean_bps = g_mean
        else:
            mean_bps = 0.0
        scores[name] = float(np.clip(mean_bps / 25.0, -3.0, 3.0))
    return scores


def select_strategy(df: pd.DataFrame, cfg: StrategyConfig | None = None, *,
                    asset: str = "crypto", current_strategy: str | None = None,
                    cooldown_active: bool = False, min_regime_bars: int = 3,
                    switch_min_advantage: float = 0.15, min_trades: int = 20,
                    costs_bps: float = 7.5, online_scores: dict[str, float] | None = None,
                    online_weight: float = 0.25, blend_enabled: bool = True,
                    blend_top_n: int = 3,
                    execution_cost_context: Mapping[str, object] | None = None,
                    precomputed_execution_costs: dict[str, tuple[pd.Series, dict[str, object]]] | None = None) -> dict:
    """Select/hold/blend using completed history and an uncertainty-aware switch test.

    The last bar is the decision bar. Strategy ranking uses prior history only.
    A challenger may replace an incumbent only when its paired net-return stream
    has a positive Newey-West HAC lead that clears a multiplicity-adjusted hurdle.
    Missing or underpowered comparisons always retain the incumbent.
    """
    if len(df) < 260:
        return {"status": "INSUFFICIENT_DATA", "strategy": "none", "regime": "UNKNOWN"}
    cfg = cfg or StrategyConfig()
    history = df.iloc[:-1].copy()
    regimes = classify_regime(df, asset=asset)
    current_regime = str(regimes.iloc[-1])
    _, stable_bars = _recent_regime_stability(regimes, min_regime_bars)
    if stable_bars < min_regime_bars:
        return {"status": "TRANSITION", "strategy": current_strategy or "none",
                "regime": current_regime, "regime_stable_bars": stable_bars,
                "reason": "regime_not_stable"}

    history_execution_context = slice_research_execution_context(
        execution_cost_context, history.index
    )
    perf = regime_conditioned_strategy_performance(
        history, cfg, costs_bps=costs_bps, asset=asset,
        execution_cost_context=history_execution_context,
        precomputed_execution_costs=precomputed_execution_costs,
    )
    ranked = []
    for name in STRATEGIES:
        m = perf.get(name, {}).get(current_regime, {})
        base = _score_metrics(m, min_trades=min_trades)
        online = float((online_scores or {}).get(name, 0.0))
        score = (1.0 - float(online_weight)) * base + float(online_weight) * online
        ranked.append({"strategy": name, "score": float(score), "historical_score": float(base),
                       "online_score": online, "metrics": m})
    ranked.sort(key=lambda x: x["score"], reverse=True)
    best = ranked[0]
    current_row = next((x for x in ranked if x["strategy"] == current_strategy), None)
    chosen = best["strategy"]
    action = "SELECT"
    switch_inference = None
    switch_hold_reason = None
    if current_row and current_strategy in STRATEGIES:
        advantage = best["score"] - current_row["score"]
        significant_lead = best["strategy"] == current_strategy
        if best["strategy"] != current_strategy:
            # Apply existing cheap switch controls before computing either expensive
            # return stream. A cooldown or inadequate score advantage already means HOLD.
            if cooldown_active:
                switch_hold_reason = "cooldown_active"
                switch_inference = {
                    "status": "SKIPPED",
                    "significant_lead": False,
                    "reason": switch_hold_reason,
                }
            elif advantage < float(switch_min_advantage):
                switch_hold_reason = "score_advantage_below_minimum"
                switch_inference = {
                    "status": "SKIPPED",
                    "significant_lead": False,
                    "reason": switch_hold_reason,
                    "score_advantage": float(advantage),
                    "minimum_score_advantage": float(switch_min_advantage),
                }
            else:
                # Compare net returns on identical bars in the regime being selected.
                # This is a paired test, not a comparison of two noisy Sharpe estimates.
                historical_regimes = classify_regime(history, asset=asset)
                regime_mask = historical_regimes == current_regime
                if history_execution_context is None:
                    challenger_stream = _strategy_returns(
                        history, cfg, best["strategy"], costs_bps, asset
                    ).loc[regime_mask]
                    incumbent_stream = _strategy_returns(
                        history, cfg, current_strategy, costs_bps, asset
                    ).loc[regime_mask]
                else:
                    challenger_stream = _strategy_returns(
                        history, cfg, best["strategy"], costs_bps, asset,
                        execution_cost_context=history_execution_context,
                        precomputed_execution_costs=precomputed_execution_costs,
                    ).loc[regime_mask]
                    incumbent_stream = _strategy_returns(
                        history, cfg, current_strategy, costs_bps, asset,
                        execution_cost_context=history_execution_context,
                        precomputed_execution_costs=precomputed_execution_costs,
                    ).loc[regime_mask]
                switch_inference = paired_return_lead(
                    challenger_stream.to_numpy(dtype=float),
                    incumbent_stream.to_numpy(dtype=float),
                    candidate_count=len(STRATEGIES),
                    alpha=0.05,
                    min_observations=60,
                    min_effective_observations=30,
                )
                significant_lead = switch_inference.get("significant_lead") is True
                if not significant_lead:
                    switch_hold_reason = str(
                        switch_inference.get("reason") or "paired_lead_not_significant"
                    )
        if cooldown_active or advantage < float(switch_min_advantage) or not significant_lead:
            chosen = current_strategy
            action = "HOLD"
        else:
            action = "SWITCH" if chosen != current_strategy else "HOLD"
    if current_regime == "TRANSITION":
        chosen = current_strategy or chosen
        action = "HOLD" if current_strategy else "ABSTAIN"

    blend = []
    blend_scope = "RANKED_POSITIVE_STRATEGIES"
    if blend_enabled and action != "ABSTAIN":
        # Restrict the blend only when HOLD rejected a different top-ranked
        # challenger. If the incumbent is already best, preserve its configured
        # ranked ensemble blend instead of silently collapsing to one strategy.
        rejected_challenger = (
            action == "HOLD"
            and current_strategy in STRATEGIES
            and best["strategy"] != current_strategy
        )
        if rejected_challenger:
            eligible = [
                row for row in ranked
                if row["strategy"] == current_strategy and row["score"] > 0
            ]
            blend_scope = "INCUMBENT_ONLY"
        else:
            eligible = [
                row for row in ranked if row["score"] > 0
            ][:max(1, int(blend_top_n))]
        if eligible:
            scores = np.asarray([x["score"] for x in eligible], dtype=float)
            # Stable softmax; never let one weak strategy dominate a strong one by noise.
            scores = scores - np.max(scores)
            weights = np.exp(scores)
            weights = weights / weights.sum()
            blend = [{"strategy": x["strategy"], "weight": float(w), "score": x["score"]}
                     for x, w in zip(eligible, weights, strict=True)]
    signal_frame = strategy_signals(df, cfg, asset=asset)
    row = signal_frame.iloc[-1]
    signals = {name: float(row.get(name, 0.0) or 0.0) for name in STRATEGIES}
    if blend:
        composite = sum(float(x["weight"]) * signals[x["strategy"]] for x in blend)
    else:
        composite = signals.get(chosen, 0.0)
    if not np.isfinite(composite):
        composite = 0.0
    return {
        "status": "OK", "action": action, "strategy": chosen, "regime": current_regime,
        "regime_stable_bars": stable_bars, "score": float(best["score"]),
        "signal": float(composite), "strategy_signals": signals, "ranked": ranked,
        "blend": blend, "selection_history_bars": int(len(history)),
        "switch_inference": switch_inference,
        "switch_hold_reason": switch_hold_reason,
        "blend_scope": blend_scope,
        "execution_cost_model_complete": bool(
            execution_cost_context is not None
            and all(
                perf.get(name, {}).get(current_regime, {}).get("execution_cost_model_complete") is True
                for name in STRATEGIES
            )
        ),
        **_execution_cost_context_audit(execution_cost_context),
        "policy": {"switch_min_advantage": float(switch_min_advantage),
                   "switch_test": "PAIRED_NEWEY_WEST_HAC",
                   "execution_cost_model_version": EXECUTION_COST_MODEL_VERSION,
                   "switch_significance_alpha": 0.05,
                   "switch_min_observations": 60,
                   "switch_min_effective_observations": 30,
                   "min_regime_bars": int(min_regime_bars), "min_trades": int(min_trades),
                   "online_weight": float(online_weight), "blend_top_n": int(blend_top_n),
                   "cooldown_active": bool(cooldown_active)},
    }


def router_walk_forward_backtest(
    df: pd.DataFrame,
    cfg: StrategyConfig | None = None,
    *,
    asset: str = "crypto",
    costs_bps: float = 7.5,
    folds: int = 5,
    min_train: int = 800,
    min_trades: int = 20,
    switch_min_advantage: float = 0.15,
    min_regime_bars: int = 3,
    execution_cost_context: Mapping[str, object] | None = None,
) -> dict:
    """Evaluate regime-conditioned selection on unseen folds and evidence-backed costs."""
    splits = purged_walk_forward_splits(
        len(df), folds=folds, min_train=min_train, purge=1, embargo=1
    )
    if not splits:
        return {"status": "INSUFFICIENT_DATA", "folds": []}
    cfg = cfg or StrategyConfig()
    # Router callers own their configured cost basis unless a verified research
    # context is explicitly supplied. Preserve venue/evidence identity so downstream
    # promotion gates can distinguish calibrated from static returns.
    context_audit = _execution_cost_context_audit(execution_cost_context)
    context_audit.update({
        "execution_cost_model_complete": execution_cost_context is not None,
        "execution_cost_model_status": (
            "CONTEXT_PROVIDED" if execution_cost_context is not None
            else "CALLER_COST_BASIS_NO_CALIBRATED_CONTEXT"
        ),
        "reason": (
            None if execution_cost_context is not None
            else "EXECUTION_COST_CONTEXT_NOT_EXPLICITLY_SUPPLIED"
        ),
    })

    signals = strategy_signals(df, cfg, asset=asset)
    regimes = classify_regime(df, asset=asset)
    precomputed_router_costs = _precompute_router_execution_costs(
        df, cfg, execution_cost_context, costs_bps=costs_bps, asset=asset
    )
    rows = []
    all_net = []
    execution_audits = []
    ranking_audits = []
    for fold in splits:
        train = df.iloc[:fold.test_start]
        if len(train) < min_train:
            continue
        train_context = slice_research_execution_context(execution_cost_context, train.index)
        perf = regime_conditioned_strategy_performance(
            train, cfg, costs_bps=costs_bps, asset=asset,
            execution_cost_context=train_context,
            precomputed_execution_costs=precomputed_router_costs,
        )
        ranking_complete = all(
            perf.get(strategy, {}).get(regime, {}).get("execution_cost_model_complete") is True
            for strategy in STRATEGIES
            for regime in perf.get(strategy, {})
        )
        ranking_audits.append(ranking_complete)
        chosen_by_regime = {}
        for regime in REGIMES:
            ranked = []
            for name in STRATEGIES:
                metrics = perf.get(name, {}).get(regime, {})
                ranked.append((name, _score_metrics(metrics, min_trades=min_trades)))
            ranked.sort(key=lambda row: row[1], reverse=True)
            chosen_by_regime[regime] = ranked[0][0] if ranked else "ensemble"

        test = df.iloc[fold.test_start:fold.test_end]
        test_context = slice_research_execution_context(execution_cost_context, test.index)
        test_regimes = regimes.iloc[fold.test_start:fold.test_end]
        # Keep the executable open-to-open return for the fold's final test bar.
        test_ret = (next_open_returns(df) - 1.0).iloc[fold.test_start:fold.test_end]
        selected = [
            chosen_by_regime.get(str(test_regimes.loc[index]), "ensemble")
            for index in test.index
        ]
        selected_series = pd.Series(selected, index=test.index)
        raw = pd.Series(0.0, index=test.index)
        for name in STRATEGIES:
            mask = selected_series == name
            if mask.any():
                raw.loc[mask] = signals.loc[test.index, name].loc[mask]
        position = raw.shift(1).fillna(0.0)
        turnover = position.diff().abs().fillna(position.abs())
        per_bar_costs, cost_audit = execution_cost_series_for_positions(
            test_context,
            position,
            test["open"].astype(float),
            namespace=f"router-walk-forward:fold:{len(rows)}",
            fallback_cost_bps=costs_bps,
        )
        net = position * test_ret - turnover * per_bar_costs / 10_000.0
        all_net.append(net)
        execution_audits.append(cost_audit)
        rows.append({
            "test_start": test.index[0].isoformat(),
            "test_end": test.index[-1].isoformat(),
            "bars": int(len(test)),
            "strategies_used": selected_series.value_counts().to_dict(),
            "total_return": float((1.0 + net).prod() - 1.0),
            "execution_cost_model_complete": cost_audit.get("execution_cost_model_complete") is True,
        })

    joined = pd.concat(all_net) if all_net else pd.Series(dtype=float)
    metrics = _metrics(joined, asset=asset)
    all_costs_complete = bool(execution_audits) and all(
        audit.get("execution_cost_model_complete") is True for audit in execution_audits
    )
    metrics.update({
        "status": "OK" if rows else "INSUFFICIENT_DATA",
        "folds": rows,
        "router": "REGIME_CONDITIONED_WALK_FORWARD",
        "execution_cost_model_complete": (
            all_costs_complete and all(ranking_audits)
        ),
        "execution_cost_model_status": (
            "VERIFIED_PER_BAR_SPREAD_VOLUME_PARTICIPATION_LATENCY"
            if execution_cost_context is not None and all_costs_complete and all(ranking_audits)
            and context_audit.get("execution_cost_calibration_verified") is True
            else "PER_BAR_EXECUTION_UNATTESTED_CONTEXT"
            if execution_cost_context is not None and all_costs_complete and all(ranking_audits)
            else "PER_BAR_EXECUTION_INCOMPLETE" if execution_cost_context is not None
            else "STATIC_FALLBACK_NO_EXECUTION_CONTEXT"
        ),
        "execution_cost_calibration_status": context_audit.get(
            "execution_cost_calibration_status"
        ),
        "execution_cost_calibration_verified": context_audit.get(
            "execution_cost_calibration_verified"
        ),
        "execution_cost_calibration_evidence_id": context_audit.get(
            "execution_cost_calibration_evidence_id"
        ),
        "execution_cost_venue": context_audit.get("execution_cost_venue"),
        "execution_cost_market_type": context_audit.get("execution_cost_market_type"),
        "execution_cost_symbol": context_audit.get("execution_cost_symbol"),
        "execution_cost_timeframe": context_audit.get("execution_cost_timeframe"),
        "execution_cost_dataset_sha256": context_audit.get("execution_cost_dataset_sha256"),
        "execution_cost_model_fingerprint": context_audit.get(
            "execution_cost_model_fingerprint"
        ),
        "per_bar_execution_context_sha256": context_audit.get(
            "per_bar_execution_context_sha256"
        ),
        "execution_cost_model_reason": context_audit.get("reason"),
        "execution_cost_failure_reasons": [
            audit.get("execution_cost_failure_reasons", {}) for audit in execution_audits
        ],
    })
    return metrics


def policy_walk_forward_backtest(
    df: pd.DataFrame,
    cfg: StrategyConfig | None = None,
    *,
    asset: str = "crypto",
    costs_bps: float = 7.5,
    folds: int = 5,
    min_train: int = 800,
    min_trades: int = 20,
    switch_min_advantage: float = 0.15,
    min_regime_bars: int = 3,
    online_weight: float = 0.25,
    cooldown_bars: int = 6,
    blend_enabled: bool = True,
    blend_top_n: int = 3,
    decision_stride: int = 4,
    execution_cost_context: Mapping[str, object] | None = None,
) -> dict:
    """Walk-forward validate adaptive routing with the same per-event cost inputs."""
    cfg = cfg or StrategyConfig()
    splits = purged_walk_forward_splits(len(df), folds=folds, min_train=min_train, purge=1, embargo=1)
    if not splits:
        return {"status": "INSUFFICIENT_DATA", "folds": []}
    # Router callers own their configured cost basis unless a verified research
    # context is explicitly supplied. Preserve venue/evidence identity so downstream
    # promotion gates can distinguish calibrated from static returns.
    context_audit = _execution_cost_context_audit(execution_cost_context)
    context_audit.update({
        "execution_cost_model_complete": execution_cost_context is not None,
        "execution_cost_model_status": (
            "CONTEXT_PROVIDED" if execution_cost_context is not None
            else "CALLER_COST_BASIS_NO_CALIBRATED_CONTEXT"
        ),
        "reason": (
            None if execution_cost_context is not None
            else "EXECUTION_COST_CONTEXT_NOT_EXPLICITLY_SUPPLIED"
        ),
    })

    signals = strategy_signals(df, cfg, asset=asset)
    regimes = classify_regime(df, asset=asset)
    precomputed_router_costs = _precompute_router_execution_costs(
        df, cfg, execution_cost_context, costs_bps=costs_bps, asset=asset
    )
    fold_rows = []
    fold_series: list[pd.Series] = []
    cost_complete_by_fold = []
    failure_reasons: dict[str, int] = {}

    for fold in splits:
        current_strategy = None
        last_switch_bar = -10**9
        observed = []
        pending_feedback = []
        previous_position = 0.0
        net_rows = []
        test_start, test_end = fold.test_start, fold.test_end
        if test_end - test_start < 3:
            continue
        stride = max(1, int(decision_stride))
        decision_points = list(range(test_start, max(test_start + 1, test_end - 2), stride))
        net_index = []
        fold_cost_complete = True
        for i in decision_points:
            still_pending = []
            for available_at, feedback in pending_feedback:
                if available_at <= i:
                    observed.append(feedback)
                else:
                    still_pending.append((available_at, feedback))
            pending_feedback = still_pending
            history = df.iloc[: i + 1]
            history_context = slice_research_execution_context(
                execution_cost_context, history.index
            )
            current_regime = str(regimes.iloc[i])
            cooldown_active = (i - last_switch_bar) < int(max(0, cooldown_bars))
            online = online_strategy_scores(
                observed, current_regime=current_regime, timeframe=None,
                half_life_days=14.0, min_observations=10,
            )
            decision = select_strategy(
                history, cfg, asset=asset, current_strategy=current_strategy,
                cooldown_active=cooldown_active, min_regime_bars=min_regime_bars,
                switch_min_advantage=switch_min_advantage, min_trades=min_trades,
                costs_bps=costs_bps, online_scores=online, online_weight=online_weight,
                blend_enabled=blend_enabled, blend_top_n=blend_top_n,
                execution_cost_context=history_context,
                precomputed_execution_costs=precomputed_router_costs,
            )
            if (
                decision.get("status") == "OK"
                and decision.get("execution_cost_model_complete") is not True
            ):
                fold_cost_complete = False
            if decision.get("status") != "OK" or decision.get("strategy") in {None, "none"}:
                target_position = 0.0
            else:
                signal = float(decision.get("signal", 0.0))
                volatility = float(signals.iloc[i].get("realized_vol") or 0.0)
                if (
                    not np.isfinite(volatility)
                    or volatility <= 0.0
                    or abs(signal) < cfg.signal_threshold
                ):
                    target_position = 0.0
                else:
                    leverage = min(cfg.max_leverage, cfg.target_vol_annual / volatility)
                    target_position = float(
                        np.clip(signal * leverage, -cfg.max_leverage, cfg.max_leverage)
                    )
                if decision.get("action") == "SWITCH":
                    last_switch_bar = i
                current_strategy = str(
                    decision.get("strategy") or current_strategy or ""
                ) or None

            next_i = min(i + stride, test_end - 2)
            entry_i = i + 1
            exit_i = next_i + 1
            if exit_i <= entry_i:
                continue
            entry_open = float(df["open"].iloc[entry_i])
            exit_open = float(df["open"].iloc[exit_i])
            if (
                not np.isfinite(entry_open)
                or not np.isfinite(exit_open)
                or entry_open <= 0.0
            ):
                raise ValueError("router walk-forward requires finite positive executable open prices")
            next_return = exit_open / entry_open - 1.0
            turnover = abs(target_position - previous_position)
            cost_rate = float(costs_bps)
            if turnover > 0.0:
                reversal = (
                    previous_position != 0.0
                    and target_position != 0.0
                    and math.copysign(1.0, previous_position) != math.copysign(1.0, target_position)
                )
                event_namespace = f"policy-router:fold:{len(fold_rows)}:event:{entry_i}"
                if reversal:
                    close_quantity = abs(previous_position)
                    open_quantity = abs(target_position)
                    close_side = "sell" if previous_position > 0.0 else "buy"
                    open_side = "buy" if target_position > 0.0 else "sell"
                    close_cost, close_reason = execution_cost_for_event(
                        execution_cost_context,
                        bar_position=entry_i,
                        turnover=close_quantity,
                        reference_price=entry_open,
                        side=close_side,
                        namespace=f"{event_namespace}:CLOSE",
                    )
                    open_cost, open_reason = execution_cost_for_event(
                        execution_cost_context,
                        bar_position=entry_i,
                        turnover=open_quantity,
                        reference_price=entry_open,
                        side=open_side,
                        namespace=f"{event_namespace}:OPEN",
                    )
                    if close_cost is None or open_cost is None:
                        fold_cost_complete = False
                        for reason in (close_reason, open_reason):
                            if reason:
                                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
                    else:
                        cost_rate = (
                            close_quantity * close_cost + open_quantity * open_cost
                        ) / turnover
                else:
                    side = "buy" if target_position - previous_position > 0.0 else "sell"
                    calibrated_cost, reason = execution_cost_for_event(
                        execution_cost_context,
                        bar_position=entry_i,
                        turnover=turnover,
                        reference_price=entry_open,
                        side=side,
                        namespace=event_namespace,
                    )
                    if calibrated_cost is None:
                        fold_cost_complete = False
                        key = reason or "EXECUTION_EVENT_UNAVAILABLE"
                        failure_reasons[key] = failure_reasons.get(key, 0) + 1
                    else:
                        cost_rate = calibrated_cost
            net = target_position * next_return - turnover * cost_rate / 10_000.0
            net_rows.append(net)
            net_index.append(df.index[exit_i])
            if target_position != 0.0:
                reward_cost_bps = (
                    turnover / abs(target_position) * cost_rate
                    if abs(target_position) > 0.0 else 0.0
                )
                reward_bps = float(
                    np.sign(target_position) * next_return * 10_000.0 - reward_cost_bps
                )
                pending_feedback.append((exit_i, {
                    "strategy": str(current_strategy or decision.get("strategy") or "ensemble"),
                    "regime": current_regime,
                    "net_return_bps": reward_bps,
                    "created_at": df.index[exit_i].to_pydatetime(),
                }))
            previous_position = target_position

        fold_net = pd.Series(net_rows, index=pd.DatetimeIndex(net_index))
        fold_series.append(fold_net)
        fold_cost_complete = fold_cost_complete and bool(execution_cost_context is not None)
        cost_complete_by_fold.append(fold_cost_complete)
        fold_metrics = _metrics(fold_net, asset=asset)
        fold_metrics.update({
            "test_start": df.index[test_start].isoformat(),
            "test_end": df.index[test_end - 1].isoformat(),
            "execution_cost_model_complete": fold_cost_complete,
        })
        fold_rows.append(fold_metrics)

    joined = pd.concat(fold_series).sort_index() if fold_series else pd.Series(dtype=float)
    overall = _metrics(joined, asset=asset)
    positive_ratio = (
        sum(float(row.get("total_return", 0.0)) > 0 for row in fold_rows) / len(fold_rows)
        if fold_rows else 0.0
    )
    overall.update({
        "status": "OK" if fold_rows else "INSUFFICIENT_DATA",
        "folds": fold_rows,
        "positive_fold_ratio": positive_ratio,
        "router": "FULL_ADAPTIVE_POLICY_WALK_FORWARD",
        "includes": ["regime", "online_outcome_feedback", "switch_hysteresis", "blending", "abstention"],
        "decision_stride_bars": int(max(1, decision_stride)),
        "execution_cost_model_complete": (
            bool(cost_complete_by_fold) and all(cost_complete_by_fold)
        ),
        "execution_cost_model_status": (
            "VERIFIED_PER_BAR_SPREAD_VOLUME_PARTICIPATION_LATENCY"
            if execution_cost_context is not None and bool(cost_complete_by_fold)
            and all(cost_complete_by_fold)
            and context_audit.get("execution_cost_calibration_verified") is True
            else "PER_BAR_EXECUTION_UNATTESTED_CONTEXT"
            if execution_cost_context is not None and bool(cost_complete_by_fold)
            and all(cost_complete_by_fold)
            else "PER_BAR_EXECUTION_INCOMPLETE" if execution_cost_context is not None
            else "STATIC_FALLBACK_NO_EXECUTION_CONTEXT"
        ),
        "execution_cost_calibration_status": context_audit.get(
            "execution_cost_calibration_status"
        ),
        "execution_cost_calibration_verified": context_audit.get(
            "execution_cost_calibration_verified"
        ),
        "execution_cost_calibration_evidence_id": context_audit.get(
            "execution_cost_calibration_evidence_id"
        ),
        "execution_cost_venue": context_audit.get("execution_cost_venue"),
        "execution_cost_market_type": context_audit.get("execution_cost_market_type"),
        "execution_cost_symbol": context_audit.get("execution_cost_symbol"),
        "execution_cost_timeframe": context_audit.get("execution_cost_timeframe"),
        "execution_cost_dataset_sha256": context_audit.get("execution_cost_dataset_sha256"),
        "execution_cost_model_fingerprint": context_audit.get(
            "execution_cost_model_fingerprint"
        ),
        "per_bar_execution_context_sha256": context_audit.get(
            "per_bar_execution_context_sha256"
        ),
        "execution_cost_model_reason": context_audit.get("reason"),
        "execution_cost_failure_reasons": failure_reasons,
    })
    return overall


def strategy_trade_plan(df: pd.DataFrame, selected_strategy: str, *, signal: float,
                        atr_multiplier_stop: float = 1.5, reward_r: float = 2.5,
                        min_reward_risk: float = 2.0, cfg: StrategyConfig | None = None,
                        asset: str = "crypto") -> dict | None:
    """Create a strategy-specific direction/levels packet.

    Risk sizing remains outside this function. This function never bypasses the
    execution risk governor.
    """
    s = strategy_signals(df, cfg or StrategyConfig(), asset=asset)
    row = s.iloc[-1]
    price = float(df.close.iloc[-1])
    atr = float(row.get("atr") or 0.0)
    if not np.isfinite(price) or not np.isfinite(atr) or atr <= 0:
        return None
    side = "buy" if signal > 0.20 else ("sell" if signal < -0.20 else "flat")
    if side == "flat":
        return None
    if selected_strategy == "breakout":
        entry = price + 0.10 * atr if side == "buy" else price - 0.10 * atr
        prior_high = float(s.iloc[-1].get("breakout_high") or price)
        prior_low = float(s.iloc[-1].get("breakout_low") or price)
        stop = min(prior_low, price - atr_multiplier_stop * atr) if side == "buy" else max(prior_high, price + atr_multiplier_stop * atr)
    elif selected_strategy == "mean_reversion":
        mean = float(df.close.rolling(20).mean().iloc[-1])
        if side == "buy" and price >= mean:
            return None
        if side == "sell" and price <= mean:
            return None
        entry = price
        stop = price - atr_multiplier_stop * atr if side == "buy" else price + atr_multiplier_stop * atr
    else:
        entry = price
        stop = price - atr_multiplier_stop * atr if side == "buy" else price + atr_multiplier_stop * atr
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    target = entry + reward_r * risk if side == "buy" else entry - reward_r * risk
    rr = abs(target - entry) / risk
    if rr < min_reward_risk:
        return None
    return {"side": side, "entry_price": float(entry), "stop_loss_price": float(stop),
            "take_profit_price": float(target), "reward_risk": float(rr), "atr": float(atr),
            "strategy": selected_strategy}
