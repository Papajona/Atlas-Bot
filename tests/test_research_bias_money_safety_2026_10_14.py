from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


def test_backtest_halt_uses_one_net_equity_curve_and_does_not_count_hypothetical_losses():
    from app.research_engine import _apply_protective_halts

    index = pd.date_range("2026-01-01", periods=16, freq="h", tz="UTC")
    # The raw strategy remains invested, but its signal losses after the first two
    # events occur while the protection system is halted; those are not executed trades.
    position = pd.Series(1.0, index=index)
    returns = pd.Series([-0.02, -0.02, -0.02, -0.02] + [0.01] * 12, index=index)
    protected, net, diagnostic = _apply_protective_halts(
        position,
        returns,
        cost_rate=0.00075,
        drawdown_limit=0.99,
        daily_loss_limit=0.99,
        loss_event_threshold=2,
        cooldown_bars=2,
        loss_lookback_bars=2,
    )

    assert protected.iloc[-1] == 1.0  # the old raw-position event counter could re-halt forever
    assert diagnostic["halted_bars"] < len(index)
    assert diagnostic["active_bars"] == int((protected != 0.0).sum())
    assert math.isclose(
        diagnostic["final_equity"],
        float(np.prod(1.0 + net.to_numpy(dtype=float))),
        rel_tol=1e-12,
        abs_tol=1e-12,
    )


def test_customer_balance_api_preserves_decimal_wire_values_and_minor_units():
    from app.customer_funds import customer_balance_api

    result = customer_balance_api({
        "available": 0.123456,
        "trading_reserved": 1.000001,
        "withdrawal_reserved": 0.000002,
        "total": 1.123459,
        "available_exact": "0.123456",
        "trading_reserved_exact": "1.000001",
        "withdrawal_reserved_exact": "0.000002",
        "total_exact": "1.123459",
    })
    # Legacy JSON keys remain numeric for existing Android/Web consumers.
    assert result["available"] == pytest.approx(0.123456)
    assert result["trading_reserved"] == pytest.approx(1.000001)
    assert result["withdrawal_reserved"] == pytest.approx(0.000002)
    assert result["total"] == pytest.approx(1.123459)
    assert result["locked"] == pytest.approx(1.000003)
    # Explicit decimal strings are authoritative for exact comparisons and round-trips.
    assert result["available_exact"] == "0.123456"
    assert result["trading_reserved_exact"] == "1.000001"
    assert result["withdrawal_reserved_exact"] == "0.000002"
    assert result["total_exact"] == "1.123459"
    assert result["locked_exact"] == "1.000003"
    assert result["available_minor_units"] == 123456
    assert result["trading_reserved_minor_units"] == 1000001
    assert result["withdrawal_reserved_minor_units"] == 2
    assert result["total_minor_units"] == 1123459
    assert result["locked_minor_units"] == 1000003
    assert result["minor_units_scale"] == 1_000_000


def test_oos_promotion_gate_requires_complete_trial_history_and_strong_evidence():
    from app.research_validation import oos_promotion_gate

    folds = [{"total_return": value} for value in (0.03, 0.02, 0.01, 0.04, 0.02, 0.01)]
    strong = {
        "status": "OK",
        "folds": folds,
        "oos_total_return": 0.12,
        "sharpe": 0.8,
        "max_drawdown": -0.10,
        "trades": 150,
        "deflated_sharpe": 0.97,
        "cost_stress_ok": True,
        "trial_registry_complete": True,
    }
    assert oos_promotion_gate(strong)["passed"] is True
    assert oos_promotion_gate({**strong, "trial_registry_complete": False})["passed"] is False
    assert oos_promotion_gate({**strong, "deflated_sharpe": 0.94})["passed"] is False
    assert oos_promotion_gate({**strong, "cost_stress_ok": False})["passed"] is False
    assert oos_promotion_gate({**strong, "trades": 99})["passed"] is False
    assert oos_promotion_gate({**strong, "max_drawdown": -0.21})["passed"] is False


@pytest.mark.parametrize("value", [None, [], "", "   ", "[]", "null", "{bad"])
def test_ai_provider_empty_or_non_object_content_returns_typed_error(value):
    from app.ai_providers import AIProviderError, _extract_json

    with pytest.raises(AIProviderError):
        _extract_json(value)


def test_json_loads_logs_corruption_before_compatibility_fallback(caplog):
    from app.main import json_loads

    with caplog.at_level(logging.WARNING):
        assert json_loads("{corrupt") == []
    assert any("invalid_json_column" in record.message for record in caplog.records)


def test_fastapi_lifecycle_uses_lifespan_not_deprecated_event_handlers():
    root = Path(__file__).resolve().parents[1]
    source = (root / "app" / "main.py").read_text(encoding="utf-8")

    assert "lifespan=app_lifespan" in source
    assert "@app.on_event(\"startup\")" not in source
    assert "@app.on_event(\"shutdown\")" not in source
    assert "await startup()" in source
    assert "await shutdown()" in source
