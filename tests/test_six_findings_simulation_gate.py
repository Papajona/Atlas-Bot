"""Deterministic synthetic-data simulations for the six AtlasRisk audit findings.

These are software-behavior tests. They do not access live venues or claim to prove
historical market coverage, venue-calibrated costs, or production readiness.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("ENVIRONMENT", "test")
ROOT = Path(__file__).resolve().parents[1]


def test_survivorship_simulation_cannot_clear_universe_evidence_limit(monkeypatch):
    from app.research_engine import paper_candidates
    from app.config import settings
    monkeypatch.setattr(settings, "research_folds", 3)

    # A synthetic, deliberately strong strategy cannot invent missing delisted-asset history.
    research = {
        "universe_bias": "CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY",
        "strategies": [{
            "strategy": "trend", "sharpe": 99.0, "max_drawdown": 0.0,
            "trades": 999, "total_return": 99.0,
        }],
        "validation": {"per_strategy_oos": {"trend": {
            "status": "OK", "oos_total_return": 0.20, "sharpe": 1.1,
            "max_drawdown": -0.10, "trades": 50,
            "folds": [{"total_return": 0.10}, {"total_return": -0.01}, {"total_return": 0.08}],
        }}},
    }
    candidate = paper_candidates(
        research,
        min_sharpe=0.0,
        max_drawdown=-0.50,
        min_trades=1,
        min_oos_total_return=0.0,
        min_oos_positive_fold_ratio=0.5,
    )[0]

    assert candidate["selection_basis"] == "OUT_OF_SAMPLE_ONLY"
    assert candidate["promotion_eligible"] is False
    assert candidate["universe_bias"] == "CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY"
    assert any("point-in-time" in item.lower() or "current_survivors" in item.lower()
               or "universe limitation" in item.lower()
               for item in candidate["evidence_limitations"])


def test_synthetic_slippage_stress_is_monotonic_but_never_claims_calibration():
    from app.paper_execution_model import estimate_paper_slippage

    context = {
        "source": "ccxt_ohlcv", "asset": "crypto", "bar_volume": 1000.0,
        "volume_unit": "base_asset", "realized_volatility_per_bar": 0.02,
        "spread_bps": 8.0,
    }
    small = estimate_paper_slippage(
        side="buy", quantity=1.0, base_slippage_bps=2.0, context=context
    )
    large = estimate_paper_slippage(
        side="buy", quantity=100.0, base_slippage_bps=2.0, context=context
    )
    volatile = estimate_paper_slippage(
        side="buy", quantity=100.0, base_slippage_bps=2.0,
        context={**context, "realized_volatility_per_bar": 0.08},
    )

    assert small["modeled_slippage_bps"] < large["modeled_slippage_bps"]
    assert large["modeled_slippage_bps"] < volatile["modeled_slippage_bps"]
    assert small["calibration_status"] == "UNVALIDATED_HEURISTIC"
    assert large["calibration_status"] == "UNVALIDATED_HEURISTIC"
    assert volatile["calibration_status"] == "UNVALIDATED_HEURISTIC"


def test_synthetic_low_liquidity_scenario_reproduces_perfect_fill_gap():
    """Audit diagnostic: passes only while this known implementation gap is present.

    For 100 requested units and 20 units of bar volume, a 10% simulated participation
    cap permits at most 2 units. The production paper path currently records all 100.
    This confirms the gap; it is not evidence that the execution simulator is fixed.
    """
    source = (ROOT / "app" / "execution.py").read_text(encoding="utf-8")
    assert 'if mode == "PAPER":' in source
    paper_block = source.split('if mode == "PAPER":', 1)[1].split("if broker is None:", 1)[0]

    requested = 100.0
    synthetic_bar_volume = 20.0
    synthetic_participation_cap = 0.10
    simulated_liquidity_limited_fill = min(requested, synthetic_bar_volume * synthetic_participation_cap)
    current_paper_path_fills = requested if "t.filled_quantity = quantity" in paper_block else None

    assert simulated_liquidity_limited_fill == pytest.approx(2.0)
    assert current_paper_path_fills == pytest.approx(100.0)
    assert "t.remaining_quantity = 0" in paper_block
    assert "t.status = \"SIMULATED\"" in paper_block


def test_selection_simulation_rejects_strong_in_sample_when_held_out_fails(monkeypatch):
    from app.research_engine import paper_candidates
    from app.config import settings
    monkeypatch.setattr(settings, "research_folds", 3)

    research = {
        "strategies": [{
            "strategy": "trend", "sharpe": 8.0, "max_drawdown": -0.01,
            "trades": 1000, "total_return": 5.0,
        }],
        "validation": {"per_strategy_oos": {"trend": {
            "status": "OK", "oos_total_return": -0.20, "sharpe": -1.5,
            "max_drawdown": -0.70, "trades": 25,
            "folds": [{"total_return": -0.10}, {"total_return": -0.15}, {"total_return": 0.01}],
        }}},
    }

    candidates = paper_candidates(
        research,
        min_sharpe=0.0,
        max_drawdown=-0.50,
        min_trades=1,
        min_oos_total_return=0.0,
        min_oos_positive_fold_ratio=0.5,
    )
    assert candidates == []


def test_customer_paper_simulation_remains_blocked_without_isolated_ledger():
    from app.execution import customer_paper_execution_blocked

    assert customer_paper_execution_blocked(1001, True) is True
    assert customer_paper_execution_blocked(1001, False) is False
    assert customer_paper_execution_blocked(None, True) is False
    source = (ROOT / "app" / "execution.py").read_text(encoding="utf-8")
    assert 'raise RiskBlocked("Customer paper execution requires an isolated simulation ledger")' in source


def test_synthetic_commodity_routing_never_uses_yahoo_fx_fallback(monkeypatch):
    from app import main

    monkeypatch.setattr(main.settings, "forex_broker", "oanda")
    monkeypatch.setattr(main.settings, "oanda_account_id", "")
    monkeypatch.setattr(main.settings, "oanda_api_token", "")
    monkeypatch.setattr(
        main, "fetch_forex",
        lambda *args, **kwargs: pytest.fail("commodity symbol incorrectly reached Yahoo FX fallback"),
    )
    request = SimpleNamespace(
        asset="commodity", symbol="XAU_USD", exchange="oanda", timeframe="1h", days=30
    )

    with pytest.raises(
        RuntimeError,
        match="Commodity market data requires configured OANDA practice credentials",
    ):
        main.market_data(request)
