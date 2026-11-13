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
