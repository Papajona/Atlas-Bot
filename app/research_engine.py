from __future__ import annotations
import json
from dataclasses import asdict
import math
import numpy as np
import pandas as pd

from .strategy_engine import StrategyConfig, strategy_signals, risk_scaled_position, target_positions
from .execution_aware_research import execution_turnover, simulate_execution_aware_strategy
from .trading_core import ai_walk_forward_backtest
from .strategy_evidence import evidence_for
from .research_validation import monte_carlo_bootstrap, parameter_plateau_score, cost_breakeven_report
from .config import settings
from .research_validation import (validate_ohlcv_frame, bootstrap_mean_ci,
                                  multiple_testing_sharpe_adjustment, cost_sensitivity,
                                  purged_walk_forward_splits, fixed_signal_walk_forward_oos,
                                  regime_conditioned_performance)


def _safe(x: float, default: float = 0.0) -> float:
    return float(x) if np.isfinite(x) else default



def _apply_protective_halts(
    position: pd.Series,
    bar_returns: pd.Series,
    *,
    cost_rate: float,
    drawdown_limit: float,
    daily_loss_limit: float,
    loss_event_threshold: int,
    cooldown_bars: int,
    loss_lookback_bars: int = 24,
) -> tuple[pd.Series, pd.Series, dict]:
    """Apply risk halts to the same net returns used for reported performance.

    A halted position is flat, but closing/re-entry turnover costs remain in the
    portfolio equity curve. Loss events are recorded only on bars where a
    protected position was actually active, so losses from hypothetical trades
    during a halt cannot perpetually extend the halt.
    """
    positions = position.fillna(0.0).astype(float)
    returns = bar_returns.reindex(positions.index).fillna(0.0).astype(float)
    active_positions: list[float] = []
    net_returns: list[float] = []
    equity = 1.0
    peak = 1.0
    day_start = 1.0
    current_day = None
    halted_until = -1
    hard_halt = False
    daily_halt = False
    halted_bars = 0
    hard_halt_reason: str | None = None
    hard_halt_triggered_at: int | None = None
    cooldown_triggers = 0
    loss_event_times: list[int] = []
    previous_position = 0.0

    for i, gross_return in enumerate(returns.to_numpy(dtype=float)):
        ts = positions.index[i]
        day = ts.date() if hasattr(ts, "date") else None
        if day != current_day:
            current_day = day
            day_start = equity
            daily_halt = False

        is_halted = hard_halt or daily_halt or i <= halted_until
        active_position = 0.0 if is_halted else float(positions.iloc[i])
        if is_halted:
            halted_bars += 1

        # This is the one authoritative net return stream. In particular, a
        # transition into a halt pays the turnover cost to flatten exposure.
        step_return = active_position * float(gross_return)
        step_return -= abs(active_position - previous_position) * float(cost_rate)
        # The equity model has limited liability; keep its arithmetic consistent
        # with the per-period compounding used by the reported metrics.
        step_return = max(-1.0, step_return)
        equity = max(0.0, equity * (1.0 + step_return))
        peak = max(peak, equity)
        drawdown = 1.0 - (equity / peak if peak else 0.0)
        daily_loss = 1.0 - (equity / day_start if day_start else 0.0)

        active_positions.append(active_position)
        net_returns.append(step_return)

        # Only actual market-loss bars while exposed count as loss events. Entry
        # fees alone are not stop-loss events, and losses during a halt are not fills.
        new_loss_event = active_position != 0.0 and (active_position * float(gross_return)) < 0.0
        if new_loss_event:
            loss_event_times.append(i)
        cutoff = i - max(1, int(loss_lookback_bars))
        loss_event_times = [event_i for event_i in loss_event_times if event_i >= cutoff]

        # A cluster cooldown is triggered only when a new real loss event reaches
        # the threshold. Otherwise old losses would repeatedly re-trigger the same
        # cooldown on every post-halt bar even when no new loss occurred.
        loss_cluster_trigger = (
            new_loss_event
            and int(loss_event_threshold) > 0
            and len(loss_event_times) >= int(loss_event_threshold)
        )
        # Peak drawdown remains latched; daily loss clears at the next UTC date.
        if drawdown >= float(drawdown_limit) and not hard_halt:
            hard_halt = True
            hard_halt_reason = "DRAWDOWN"
            hard_halt_triggered_at = i
        elif daily_loss >= float(daily_loss_limit) and not daily_halt:
            daily_halt = True
            if hard_halt_reason is None:
                hard_halt_reason = "DAILY_LOSS"
                hard_halt_triggered_at = i
        elif not is_halted and loss_cluster_trigger:
            halted_until = max(halted_until, i + max(0, int(cooldown_bars)))
            cooldown_triggers += 1
        previous_position = active_position

    protected_position = pd.Series(active_positions, index=positions.index, dtype=float)
    protected_net = pd.Series(net_returns, index=positions.index, dtype=float)
    return protected_position, protected_net, {
        "final_equity": equity,
        "halted_bars": halted_bars,
        "active_bars": int((protected_position != 0.0).sum()),
        "loss_events": len(loss_event_times),
        "hard_halt": hard_halt,
        "hard_halt_reason": hard_halt_reason,
        "hard_halt_triggered_at": hard_halt_triggered_at,
        "cooldown_until": halted_until,
        "cooldown_triggers": cooldown_triggers,
    }


def _protected_component_position(
    df: pd.DataFrame,
    component: str,
    cfg: StrategyConfig,
    taker_bps: float,
    slippage_bps: float,
    asset: str = "crypto",
    carry_bps_per_bar: float | None = None,
):
    """Return one execution-aware, costed, risk-controlled stream for all research metrics."""
    signals = strategy_signals(df, cfg, asset=asset)
    raw = signals[component].fillna(0.0)
    requested = target_positions(raw, signals["realized_vol"], cfg).shift(1).fillna(0.0)
    carry = carry_bps_per_bar
    if carry is None:
        carry = settings.research_carry_bps_per_bar
    execution = simulate_execution_aware_strategy(
        df,
        requested,
        signals["atr"].shift(1),
        stop_atr=cfg.stop_atr,
        take_profit_atr=cfg.take_profit_atr,
        transaction_cost_bps=float(taker_bps) + float(slippage_bps),
        carry_bps_per_bar=carry,
        drawdown_limit=settings.strategy_drawdown_halt,
        daily_loss_limit=settings.strategy_daily_loss_halt,
        loss_event_threshold=settings.strategy_stoploss_guard_trades,
        cooldown_bars=settings.strategy_stoploss_guard_bars,
    )
    return (
        signals, raw, requested, execution["market_returns"], execution["positions"],
        execution["net_returns"], execution["audit"], execution["intrabar_exit"],
    )

def _component_backtest(df: pd.DataFrame, component: str, cfg: StrategyConfig, taker_bps: float, slippage_bps: float, asset: str = "crypto", carry_bps_per_bar: float | None = None) -> dict:
    """Cost-aware component backtest with forward-looking risk protections."""
    s, _, _, _, protected, protected_net, protection_audit, _ = _protected_component_position(
        df, component, cfg, taker_bps, slippage_bps, asset, carry_bps_per_bar
    )
    ann = float(s.attrs.get("annualization_factor", 252.0))
    active = protected != 0
    protected_equity = (1.0 + protected_net).cumprod()
    reported_equity = float(protected_equity.iloc[-1]) if len(protected_equity) else 1.0
    if not math.isclose(reported_equity, float(protection_audit["final_equity"]), rel_tol=1e-10, abs_tol=1e-12):
        raise RuntimeError("Research risk-control equity differs from the reported protected return curve")
    protected_peak = protected_equity.cummax()
    protected_dd = protected_equity / protected_peak - 1.0
    std = float(protected_net.std())
    return {
        "strategy": component,
        "bars": int(len(df)),
        "validated_bars": int(s[[component, "atr", "realized_vol"]].notna().all(axis=1).sum()),
        "total_return": _safe(protected_equity.iloc[-1] - 1.0),
        "max_drawdown": _safe(protected_dd.min()),
        "sharpe": _safe(protected_net.mean() / std * math.sqrt(ann)) if std > 0 else 0.0,
        "trades": int(((protected != 0) & ((protected.shift(1).fillna(0.0) == 0.0) | (np.sign(protected) != np.sign(protected.shift(1).fillna(0.0))))).sum()),
        "hit_rate": _safe((protected_net[active] > 0).mean()) if bool(active.any()) else 0.0,
        "average_leverage": _safe(protected.abs().mean()),
        "max_leverage": _safe(protected.abs().max()),
        "protected_active_bars": protection_audit["active_bars"],
        "risk_halted_bars": protection_audit["risk_halted_bars"],
        "stop_loss_exits": protection_audit["stop_loss_exits"],
        "take_profit_exits": protection_audit["take_profit_exits"],
        "gap_exits": protection_audit["gap_exits"],
        "ambiguous_bars_filled_stop_first": protection_audit["ambiguous_bars_filled_stop_first"],
        "carry_bps_per_bar": protection_audit["carry_bps_per_bar"],
        "carry_rate_configured": protection_audit["carry_bps_per_bar"] is not None,
        "carry_evidence_source": str(settings.research_carry_evidence_source or "").strip() or None,
        "carry_evidence_id": str(settings.research_carry_evidence_id or "").strip() or None,
        "financing_cost_model_complete": (
            protection_audit["carry_bps_per_bar"] is not None
            and bool(str(settings.research_carry_evidence_source or "").strip())
            and bool(str(settings.research_carry_evidence_id or "").strip())
        ),
        "intrabar_stop_target_model_complete": protection_audit["intrabar_stop_target_model_complete"],
        "transaction_cost_bps": float(taker_bps + slippage_bps),
        "annualization_factor": ann,
        "risk_protections": {
            "drawdown_halt": settings.strategy_drawdown_halt,
            "daily_loss_halt": settings.strategy_daily_loss_halt,
            "loss_event_guard": settings.strategy_stoploss_guard_trades,
            "cooldown_bars": settings.strategy_stoploss_guard_bars,
            "note": "OHLC ATR stop and target exits use conservative stop-first handling when both barriers are touched; loss clusters are a separate portfolio halt heuristic.",
        },
    }


def _regime_snapshot(df: pd.DataFrame, cfg: StrategyConfig, asset: str = "crypto") -> dict:
    s = strategy_signals(df, cfg, asset=asset)
    c = df["close"].astype(float)
    fast = c.ewm(span=cfg.trend_fast, adjust=False).mean()
    slow = c.ewm(span=cfg.trend_slow, adjust=False).mean()
    trend_strength = abs(float(fast.iloc[-1] / slow.iloc[-1] - 1.0)) if len(df) else 0.0
    vol = float(s["realized_vol"].dropna().iloc[-1]) if s["realized_vol"].notna().any() else 0.0
    if trend_strength >= 0.01 and float(fast.iloc[-1]) > float(slow.iloc[-1]):
        regime = "TREND_UP"
    elif trend_strength >= 0.01 and float(fast.iloc[-1]) < float(slow.iloc[-1]):
        regime = "TREND_DOWN"
    elif vol >= 0.60:
        regime = "HIGH_VOLATILITY_RANGE"
    else:
        regime = "RANGE_OR_LOW_TREND"
    return {"regime": regime, "annualized_realized_vol": _safe(vol), "trend_strength": _safe(trend_strength)}


def _rank_for_research(rows: list[dict]) -> list[dict]:
    # This is a research ordering, not a recommendation. It rewards risk-adjusted
    # evidence while penalizing severe drawdown and insufficient observations.
    for r in rows:
        r["research_score"] = _safe(
            0.45 * r["sharpe"] + 2.0 * r["total_return"] + 0.15 * r["hit_rate"] +
            0.10 * r["trades"] / max(r["bars"], 1) - 0.60 * abs(min(r["max_drawdown"], 0.0))
        )
    return sorted(rows, key=lambda x: x["research_score"], reverse=True)


def _has_valid_oos_measurement(metrics: dict) -> bool:
    if not isinstance(metrics, dict) or metrics.get("status") != "OK":
        return False
    try:
        return bool(np.isfinite(float(metrics.get("oos_total_return"))))
    except (TypeError, ValueError, OverflowError):
        return False


def _rank_by_oos(rows: list[dict], oos_by_strategy: dict[str, dict]) -> list[dict]:
    """Order measured OOS results first; place missing/invalid OOS results last."""
    def finite_or_negative_infinity(value) -> float:
        try:
            number = float(value)
            return number if np.isfinite(number) else float("-inf")
        except (TypeError, ValueError, OverflowError):
            return float("-inf")

    def key(row: dict) -> tuple[bool, float, float]:
        metrics = oos_by_strategy.get(str(row.get("strategy") or ""), {})
        valid = _has_valid_oos_measurement(metrics)
        if not valid:
            return (False, float("-inf"), float("-inf"))
        return (
            True,
            finite_or_negative_infinity(metrics.get("oos_total_return")),
            finite_or_negative_infinity(metrics.get("sharpe")),
        )
    return sorted(rows, key=key, reverse=True)


def backtest_all_strategies(df: pd.DataFrame, cfg: StrategyConfig | None = None,
                            taker_bps: float = 5.5, slippage_bps: float = 2.0,
                            include_ai: bool = True, asset: str = "crypto",
                            folds: int = 5, min_train: int = 800,
                            ai_threshold: float = 0.05,
                            prior_trial_labels=None,
                            carry_bps_per_bar: float | None = None) -> dict:
    cfg = cfg or StrategyConfig()
    carry = carry_bps_per_bar if carry_bps_per_bar is not None else settings.research_carry_bps_per_bar
    carry_evidence_source = str(settings.research_carry_evidence_source or "").strip()
    carry_evidence_id = str(settings.research_carry_evidence_id or "").strip()
    carry_evidence_complete = carry is not None and bool(carry_evidence_source) and bool(carry_evidence_id)
    data_quality = validate_ohlcv_frame(df)
    # The current established-coin allowlist is a live liquidity control, not a
    # historical point-in-time universe. Carry this limitation into every research result.
    universe_bias = (
        "CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY"
        if str(asset).lower() == "crypto" else "NOT_APPLICABLE"
    )
    data_quality["universe_bias"] = universe_bias
    provenance = dict(df.attrs.get("data_provenance") or {})
    if not provenance.get("sha256"):
        data_quality.setdefault("issues", []).append("missing_data_provenance_hash")
        data_quality["valid"] = False
    if not data_quality["valid"]:
        return {"research_version": "3.10.3-validation-v2", "status": "DATA_INVALID",
                "data_quality": data_quality, "data_provenance": provenance,
                "universe_bias": universe_bias, "strategies": [], "strategy_count": 0}
    components = ["trend", "momentum", "breakout", "mean_reversion", "ensemble"]
    results = [_component_backtest(df, c, cfg, taker_bps, slippage_bps, asset, carry) for c in components]
    for row in results:
        row["evidence"] = evidence_for(row["strategy"])
    validation_splits = purged_walk_forward_splits(len(df), folds=folds, min_train=min_train, purge=1, embargo=1)
    signal_frame = strategy_signals(df, cfg, asset=asset)
    annualization_factor = float(signal_frame.attrs.get("annualization_factor", 252.0))
    _, _, _, bar_returns, ensemble_pos, ensemble_net, ensemble_audit, ensemble_exits = _protected_component_position(
        df, "ensemble", cfg, taker_bps, slippage_bps, asset, carry
    )
    gross = ensemble_pos * bar_returns
    turnover = execution_turnover(ensemble_pos, ensemble_exits)
    gross_after_carry = gross if carry is None else gross - ensemble_pos.abs() * (float(carry) / 10000.0)
    sensitivity = cost_sensitivity(gross_after_carry.to_numpy(), turnover.to_numpy())
    ci = bootstrap_mean_ci(ensemble_net.to_numpy(), block_size=10)
    adjusted = multiple_testing_sharpe_adjustment([r.get("sharpe", 0.0) for r in results], trials=len(results))
    cost_mult = float(getattr(settings, "research_cost_stress_multiplier", 2.0))
    oos = fixed_signal_walk_forward_oos(
        df, ensemble_pos, costs_bps=taker_bps + slippage_bps,
        folds=folds, min_train=min_train,
        annualization_factor=annualization_factor,
        taker_bps=taker_bps, slippage_bps=slippage_bps,
        cost_stress_multiplier=cost_mult, positions_are_final=True,
        bar_returns=bar_returns, intrabar_exit=ensemble_exits, carry_bps_per_bar=carry,
        carry_evidence_source=carry_evidence_source, carry_evidence_id=carry_evidence_id,
    )
    regime_performance = regime_conditioned_performance(df, ensemble_net, asset=asset)
    per_strategy_oos = {}
    for component in components:
        _, _, _, component_returns, component_position, _, _, component_exits = _protected_component_position(
            df, component, cfg, taker_bps, slippage_bps, asset, carry
        )
        per_strategy_oos[component] = fixed_signal_walk_forward_oos(
            df, component_position, costs_bps=taker_bps + slippage_bps,
            folds=folds, min_train=min_train,
            annualization_factor=annualization_factor,
            taker_bps=taker_bps, slippage_bps=slippage_bps,
            cost_stress_multiplier=cost_mult, positions_are_final=True,
            bar_returns=component_returns, intrabar_exit=component_exits,
            carry_bps_per_bar=carry,
            carry_evidence_source=carry_evidence_source, carry_evidence_id=carry_evidence_id,
        )
    # Parameter-plateau check: small changes to the signal threshold should not destroy
    # the measured edge. A narrow single-point optimum is treated as fragile research.
    variants=[]
    for mult in (0.80,0.90,1.00,1.10,1.20):
        vcfg=StrategyConfig(**{**asdict(cfg), "signal_threshold": float(cfg.signal_threshold*mult)})
        vr=_component_backtest(df,"ensemble",vcfg,taker_bps,slippage_bps,asset,carry)
        variants.append({"multiplier":mult,"sharpe":vr.get("sharpe",0.0),"total_return":vr.get("total_return",0.0),"max_drawdown":vr.get("max_drawdown",0.0)})
    robustness={"monte_carlo":monte_carlo_bootstrap(ensemble_net.to_numpy(),trials=500),"parameter_plateau":parameter_plateau_score({"variants":variants},metric="sharpe",tolerance=0.20),"parameter_variants":variants}
    # Formal DSR. Count every variant evaluated on this data: components + parameter variants (+ AI when included).
    net_ensemble = ensemble_net.to_numpy()
    # Persistent trial accounting: count DISTINCT configurations ever evaluated (label = strategy/variant + config fingerprint), not runs.
    # Re-running the same configs on one more day of data is not a new trial; changing any parameter or adding a strategy is.
    import hashlib
    # Use a full SHA-256 fingerprint so config variants cannot silently collide
    # because a short fingerprint was truncated to only 32 bits.
    fp = hashlib.sha256(json.dumps(asdict(cfg), default=str, sort_keys=True).encode()).hexdigest()
    this_labels = {f"{r.get('strategy')}@{fp}" for r in results} | {f"variant:{v['multiplier']}@{fp}" for v in variants}
    if include_ai:
        this_labels.add(f"ai@{fp}")
    # A set of trial labels proves only that configurations were named in prior runs.
    # It does not provide the historical trial Sharpe distribution evaluated on the
    # same OOS observations, which DSR needs to correct selection bias. Never promote
    # from labels/counts alone; the trusted research archive and its comparable trial
    # outcomes must be restored and verified first.
    known_prior_labels = set(prior_trial_labels or ())
    n_trials = len(known_prior_labels | this_labels)
    trial_registry_complete = False
    dsr = {
        "status": "MISSING_COMPARABLE_TRIAL_PERFORMANCE_HISTORY",
        "observations": int(np.isfinite(np.asarray(net_ensemble, dtype=float)).sum()),
        "trial_count_current_run": len(this_labels),
        "known_prior_trial_label_count": len(known_prior_labels),
        "trial_count_known_lower_bound": n_trials,
        "note": (
            "Prior trial labels do not contain historical Sharpe outcomes measured on "
            "the same OOS sample; a complete trusted experiment archive and chronological "
            "re-evaluation are required before promotion-grade DSR can be computed."
        ),
    }
    oos["trial_registry_complete"] = trial_registry_complete
    if dsr.get("status") == "OK":
        oos["deflated_sharpe"] = dsr["deflated_sharpe_ratio"]
    # Full-sample cost analysis is diagnostic only; the promotion gate consumes
    # the OOS-only stress results attached by fixed_signal_walk_forward_oos.
    ens_cost = cost_breakeven_report(
        gross.to_numpy(), turnover.to_numpy(),
        taker_bps=taker_bps, slippage_bps=slippage_bps,
        bars_per_year=max(1, int(round(annualization_factor))),
        stress_multiplier=cost_mult,
        fixed_cost_drag=None if carry is None else (ensemble_pos.abs() * (float(carry) / 10000.0)).to_numpy(),
    )
    ens_cost["carry_rate_configured"] = carry is not None
    ens_cost["carry_evidence_source_present"] = bool(carry_evidence_source)
    ens_cost["carry_evidence_id_present"] = bool(carry_evidence_id)
    ens_cost["financing_cost_model_complete"] = carry_evidence_complete
    ens_cost["intrabar_stop_target_model_complete"] = True
    if not carry_evidence_complete:
        ens_cost["note"] += " Financing promotion evidence requires a configured carry rate and traceable carry source/evidence ID."
    # Each component is judged on its own net returns (main.py gates on per_strategy_oos[selected]).
    for component in components:
        # Component Sharpe values cannot be deflated from prior labels alone either.
        comp_dsr = {
            "status": "MISSING_COMPARABLE_TRIAL_PERFORMANCE_HISTORY",
            "observations": int(per_strategy_oos[component].get("oos_bars", 0)),
            "trial_count_current_run": len(this_labels),
            "known_prior_trial_label_count": len(known_prior_labels),
            "trial_count_known_lower_bound": n_trials,
            "note": (
                "Prior trial labels do not contain historical Sharpe outcomes measured on "
                "the same OOS sample; trusted trial-performance evidence and chronological "
                "re-evaluation are required before promotion-grade DSR can be computed."
            ),
        }
        per_strategy_oos[component]["trial_registry_complete"] = trial_registry_complete
        per_strategy_oos[component]["deflated_sharpe_detail"] = comp_dsr
        if comp_dsr.get("status") == "OK":
            per_strategy_oos[component]["deflated_sharpe"] = comp_dsr["deflated_sharpe_ratio"]
    ai_result = None
    if include_ai:
        try:
            ai_result = ai_walk_forward_backtest(df, asset, folds, min_train, ai_threshold)
        except Exception as exc:
            ai_result = {"strategy": "ai_walk_forward", "status": "INSUFFICIENT_DATA", "error": str(exc)}
    in_sample_ranked = _rank_for_research(results.copy())
    # Headline order is based on held-out metrics; invalid OOS results always rank last.
    oos_ranked = _rank_by_oos(results.copy(), per_strategy_oos)
    return {
        "research_version": "3.10.3-validation-v2",
        "data_quality": data_quality,
        "data_provenance": provenance,
        "universe_bias": universe_bias,
        "validation": {"purged_walk_forward_folds": [fold.__dict__ for fold in validation_splits],
                        "cost_sensitivity": sensitivity,
                        "bootstrap_mean_return_ci": ci,
                        "multiple_testing_sharpe_proxy": adjusted,
                        "deflated_sharpe": dsr,
                        "trial_labels": sorted(this_labels),
                        "trial_registry_complete": trial_registry_complete,
                        "financing_cost_model_complete": carry_evidence_complete,
                        "carry_rate_configured": carry is not None,
                        "carry_evidence_source": carry_evidence_source or None,
                        "carry_evidence_id": carry_evidence_id or None,
                        "intrabar_stop_target_model_complete": True,
                        "execution_cost_model_complete": bool(carry_evidence_complete),
                        "carry_bps_per_bar": carry,
                        "trial_registry_status": "COMPLETE" if trial_registry_complete else "MISSING_COMPARABLE_TRIAL_PERFORMANCE_HISTORY",
                        "cumulative_trials": n_trials,
                        "cost_analysis": ens_cost,
                        "fixed_signal_oos": oos,
                        "per_strategy_oos": per_strategy_oos,
                        "regime_conditioned_performance": regime_performance},
        "strategy_count": len(results) + (1 if ai_result else 0),
        "strategies": results,
        "research_order": [r["strategy"] for r in oos_ranked],
        "research_order_basis": "OOS_RETURN_THEN_SHARPE_NON_OK_LAST",
        "in_sample_rank_order": [r["strategy"] for r in in_sample_ranked],
        "in_sample_rank_order_basis": "IN_SAMPLE_DIAGNOSTIC_ONLY_NOT_PROMOTION_EVIDENCE",
        "ai_walk_forward": ai_result,
        "regime": _regime_snapshot(df, cfg, asset=asset),
        "robustness": robustness,
        "config": asdict(cfg),
    }


def paper_candidates(
    research: dict,
    min_sharpe: float | None = None,
    max_drawdown: float | None = None,
    min_trades: int | None = None,
    min_oos_total_return: float | None = None,
    min_oos_positive_fold_ratio: float | None = None,
) -> list[dict]:
    """Select paper candidates using both historical and forward-only evidence.

    In-sample metrics alone are never sufficient. The OOS gate is intentionally
    simple and transparent; it is not a guarantee of future performance.
    """
    min_sharpe = settings.adaptive_min_sharpe if min_sharpe is None else float(min_sharpe)
    max_drawdown = settings.adaptive_max_drawdown if max_drawdown is None else float(max_drawdown)
    min_trades = settings.adaptive_min_trades if min_trades is None else int(min_trades)
    min_oos_total_return = settings.research_min_oos_total_return if min_oos_total_return is None else float(min_oos_total_return)
    min_oos_positive_fold_ratio = settings.research_min_oos_positive_fold_ratio if min_oos_positive_fold_ratio is None else float(min_oos_positive_fold_ratio)
    # A crypto allowlist of assets that survive today is not a historical point-in-time
    # universe. No trusted point-in-time universe loader/manifest validator is implemented
    # in this code path, so a caller-supplied "verified" string cannot substitute for evidence.
    # Until that pipeline exists, only NOT_APPLICABLE data may produce candidates.
    universe_bias = research.get("universe_bias") or (research.get("data_quality") or {}).get("universe_bias", "UNKNOWN")
    if universe_bias != "NOT_APPLICABLE":
        return []
    candidates = []
    oos_all = research.get("validation", {}).get("per_strategy_oos", {})
    for row in research.get("strategies", []):
        name = row.get("strategy")
        oos = oos_all.get(name, {})
        folds = oos.get("folds", []) or []
        positive_ratio = (sum(1 for f in folds if float(f.get("total_return", 0.0)) > 0) / len(folds)) if folds else 0.0
        if (row.get("sharpe", 0.0) >= min_sharpe and
                row.get("max_drawdown", 0.0) >= max_drawdown and
                row.get("trades", 0) >= min_trades and
                row.get("total_return", 0.0) > 0 and
                oos.get("status") == "OK" and
                float(oos.get("oos_total_return", -1.0)) >= min_oos_total_return and
                positive_ratio >= min_oos_positive_fold_ratio):
            candidates.append({"strategy": name, "reason": "in_sample_and_forward_gate_passed",
                               "metrics": row, "oos": oos,
                               "oos_positive_fold_ratio": positive_ratio})
    return candidates


def live_strategy_signals(df: pd.DataFrame, strategies: list[str], cfg: StrategyConfig | None = None, asset: str = "crypto") -> list[dict]:
    cfg = cfg or StrategyConfig()
    s = strategy_signals(df, cfg, asset=asset)
    latest = s.iloc[-1]
    price = float(df["close"].iloc[-1])
    output = []
    for name in strategies:
        if name not in {"trend", "momentum", "breakout", "mean_reversion", "ensemble"}:
            continue
        raw = float(latest[name]) if np.isfinite(latest[name]) else 0.0
        vol = float(latest["realized_vol"]) if np.isfinite(latest["realized_vol"]) else 0.0
        position = risk_scaled_position(raw, vol, cfg) if abs(raw) >= cfg.signal_threshold else 0.0
        output.append({
            "strategy": name, "timestamp": s.index[-1].isoformat(), "price": price,
            "signal": raw, "position_leverage": position,
            "side": "buy" if position > 0 else ("sell" if position < 0 else "flat"),
            "realized_vol_annual": vol, "mode": "PAPER_SHADOW_ONLY"
        })
    return output


def ai_market_review(research: dict, live_signal: dict | None = None) -> dict:
    rows = research.get("strategies", [])
    regime = research.get("regime", {})
    validation = research.get("validation") or {}
    oos_by_strategy = validation.get("per_strategy_oos") or {}
    if not rows:
        return {"status": "NO_DATA", "summary": "No strategy research results are available."}

    ranked_oos = _rank_by_oos(rows, oos_by_strategy)
    measured = [row for row in ranked_oos if _has_valid_oos_measurement(oos_by_strategy.get(str(row.get("strategy") or ""), {}))]
    best = measured[0] if measured else None

    def _finite_metric(strategy_name: str, metric_name: str, default: float = 0.0) -> float:
        try:
            value = float((oos_by_strategy.get(strategy_name) or {}).get(metric_name))
            return value if np.isfinite(value) else default
        except (TypeError, ValueError, OverflowError):
            return default

    profitable_oos = [
        row for row in measured
        if _finite_metric(str(row.get("strategy") or ""), "oos_total_return") > 0
        and _finite_metric(str(row.get("strategy") or ""), "sharpe") > 0
    ]
    drawdown_warnings = [
        str(row["strategy"]) for row in measured
        if _finite_metric(str(row.get("strategy") or ""), "max_drawdown") < -0.25
    ]
    summary = (
        f"Current regime classified as {regime.get('regime', 'UNKNOWN')}. "
        f"The research set contains {len(rows)} rule-based strategy variants; "
        f"{len(profitable_oos)} measured out-of-sample results showed positive net return and Sharpe."
    )
    observations = [
        (
            f"Highest measured out-of-sample result (not promotion approval): {best['strategy']} "
            f"(net return {_finite_metric(str(best['strategy']), 'oos_total_return'):.3f}, "
            f"Sharpe {_finite_metric(str(best['strategy']), 'sharpe'):.3f})."
            if best else "No strategy has a valid out-of-sample measurement; no headline winner is reported."
        ),
        f"Current annualized realized volatility: {regime.get('annualized_realized_vol', 0):.3f}.",
    ]
    if drawdown_warnings:
        observations.append("Large out-of-sample drawdowns detected in: " + ", ".join(drawdown_warnings) + ".")
    if live_signal:
        observations.append(
            f"Live ensemble signal is {live_signal.get('side', 'unknown')} with score "
            f"{float(live_signal.get('ensemble_score', 0)):.3f}; this is context, not a trade recommendation."
        )
    universe_bias = research.get("universe_bias") or (research.get("data_quality") or {}).get("universe_bias", "UNKNOWN")
    return {
        "status": "REVIEW_READY",
        "summary": summary,
        "observations": observations,
        "regime": regime,
        "top_strategy": str(best["strategy"]) if best else "",
        "top_strategy_basis": "OUT_OF_SAMPLE_MEASUREMENT_ONLY",
        "top_strategy_oos_status": "OK" if best else "NO_VALID_OOS_MEASUREMENT",
        "research_order": research.get("research_order", []),
        "research_order_basis": research.get("research_order_basis", "OOS_ONLY"),
        "universe_bias": universe_bias,
        "paper_candidates": paper_candidates(research),
        "limitations": [
            "Historical backtests do not guarantee future performance.",
            "Walk-forward validation reduces but does not eliminate overfitting.",
            "An out-of-sample measurement is not promotion approval; all promotion and risk gates must still pass.",
            f"Research universe limitation: {universe_bias}.",
            "Live promotion should require paper/shadow evidence and risk gates.",
        ],
    }
