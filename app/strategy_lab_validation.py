"""Promotion checks for Strategy Lab candidates.

Selected-on backtest metrics are diagnostics only. A Lab candidate may pass
only from the separately measured held-out OOS gate and OOS metrics.
"""
from __future__ import annotations

import math
from typing import Any


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


UNCOMPILED_CANDIDATE_SAFEGUARDS = frozenset({
    "HIGHER_TIMEFRAME_AGREEMENT",
    "CONSERVATIVE_VOLATILITY_CAP",
    "TIME_STOP",
    "REGIME_ADAPTATION",
    "VOLATILITY_SCALED_RISK",
})


def uncompiled_candidate_safeguards(spec: Any) -> list[str]:
    """List Native AI profile safeguards not yet executed by the Strategy Lab backtester.

    These rule objects are descriptive StrategyCondition values, not executable filters.
    Until the backtest compiles and exercises them, a candidate containing one must not
    receive a VALIDATED result based only on the base strategy-family/stop/target test.
    """
    if not isinstance(spec, dict):
        return []
    found: set[str] = set()
    for field in ("entry_conditions", "exit_conditions"):
        conditions = spec.get(field)
        if not isinstance(conditions, list):
            continue
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            condition_type = str(condition.get("type") or "").strip().upper()
            if condition_type in UNCOMPILED_CANDIDATE_SAFEGUARDS:
                found.add(condition_type)
    return sorted(found)


def record_validation_unavailable_reason(gate: dict[str, Any], reason: str) -> None:
    """Preserve every independent reason a candidate cannot be validated.

    Keep the first reason as the backwards-compatible primary field while exposing
    the complete ordered list to API callers and the append-only validation history.
    """
    if not isinstance(gate, dict):
        raise TypeError("validation gate must be a dictionary")
    normalized = str(reason or "").strip()
    if not normalized:
        raise ValueError("validation-unavailable reason must not be empty")
    existing = gate.get("validation_unavailable_reason")
    prior = gate.get("validation_unavailable_reasons")
    reasons: list[str] = []
    candidates = [existing]
    if isinstance(prior, list):
        candidates.extend(prior)
    candidates.append(normalized)
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value and value not in reasons:
            reasons.append(value)
    gate.setdefault("validation_unavailable_reason", normalized)
    gate["validation_unavailable_reasons"] = reasons


def strategy_lab_candidate_passes(
    oos_gate: dict,
    *,
    validation_available: bool,
    min_sharpe: float,
    max_drawdown: float,
    min_trades: int,
    min_total_return: float,
) -> tuple[bool, dict[str, float | int | None]]:
    """Apply candidate thresholds exclusively to the held-out OOS gate result."""
    source = oos_gate if isinstance(oos_gate, dict) else {}
    sharpe = _finite(source.get("sharpe"))
    drawdown = _finite(source.get("max_drawdown"))
    raw_trades = _finite(source.get("trades"))
    trades = (
        int(raw_trades)
        if raw_trades is not None and raw_trades >= 0 and raw_trades.is_integer()
        else None
    )
    total_return = _finite(source.get("oos_total_return"))
    metrics: dict[str, float | int | None] = {
        "sharpe": sharpe,
        "max_drawdown": drawdown,
        "trades": trades,
        "total_return": total_return,
    }
    passed = bool(
        validation_available
        and source.get("passed") is True
        and sharpe is not None and sharpe >= float(min_sharpe)
        and drawdown is not None and drawdown >= -abs(float(max_drawdown))
        and trades is not None and trades >= int(min_trades)
        and total_return is not None and total_return >= float(min_total_return)
    )
    return passed, metrics
