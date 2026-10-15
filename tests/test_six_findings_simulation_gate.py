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


def test_synthetic_low_liquidity_scenario_now_produces_partial_fills_without_reusing_a_bar():
    from app.paper_execution_model import simulate_paper_fill_step

    context = {
        "source": "ccxt_ohlcv", "asset": "crypto", "bar_volume": 20.0,
        "volume_unit": "base_asset", "realized_volatility_per_bar": 0.02,
        "spread_bps": 6.0, "bar_timestamp": "2026-10-15T12:00:00Z",
    }
    fill = simulate_paper_fill_step(
        remaining_quantity=100.0, context=context, order_key="stress-order-001",
        last_processed_bar_timestamp="2026-10-15T11:00:00Z", bars_waited=0,
        latency_bars=1, max_bar_volume_participation=0.10,
        queue_fill_probability=1.0, rejection_probability=0.0,
    )
    repeated = simulate_paper_fill_step(
        remaining_quantity=fill["remaining_quantity"], context=context,
        order_key="stress-order-001",
        last_processed_bar_timestamp=fill["last_processed_bar_timestamp"],
        bars_waited=fill["bars_waited"], latency_bars=1,
        max_bar_volume_participation=0.10,
        queue_fill_probability=1.0, rejection_probability=0.0,
    )

    assert fill["event"] == "PARTIAL_FILL"
    assert 0.0 < fill["fill_quantity"] <= 2.0
    assert fill["remaining_quantity"] == pytest.approx(100.0 - fill["fill_quantity"])
    assert fill["remaining_quantity"] > 98.0
    assert repeated["event"] == "NO_NEW_BAR"
    assert repeated["fill_quantity"] == 0.0


def test_stress_orders_share_one_bar_liquidity_budget():
    from app.paper_execution_model import simulate_paper_fill_step

    bar_volume = 20.0
    bar_participation = 0.10
    aggregate_cap = bar_volume * bar_participation
    used = 0.0
    context = {
        "source": "ccxt_ohlcv", "asset": "crypto", "bar_volume": bar_volume,
        "volume_unit": "base_asset", "realized_volatility_per_bar": 0.03,
        "spread_bps": 5.0, "bar_timestamp": "2026-10-15T13:00:00Z",
    }
    fills = []
    for index in range(500):
        available = max(0.0, aggregate_cap - used)
        result = simulate_paper_fill_step(
            remaining_quantity=100.0, context=context, order_key=f"stress-{index:04d}",
            last_processed_bar_timestamp="2026-10-15T12:00:00Z", bars_waited=0,
            latency_bars=1, max_bar_volume_participation=bar_participation,
            queue_fill_probability=1.0, rejection_probability=0.0,
            max_fill_quantity=available,
        )
        used += result["fill_quantity"]
        fills.append(result["fill_quantity"])
    assert any(fill > 0 for fill in fills)
    assert used <= aggregate_cap + 1e-12
    assert used <= 2.0 + 1e-12


def test_fill_simulator_models_latency_queue_misses_rejections_and_unverified_volume():
    from app.paper_execution_model import simulate_paper_fill_step

    base = {
        "source": "ccxt_ohlcv", "asset": "crypto", "bar_volume": 100.0,
        "volume_unit": "base_asset", "realized_volatility_per_bar": 0.02,
        "spread_bps": 4.0,
    }
    latency = simulate_paper_fill_step(
        remaining_quantity=10.0,
        context={**base, "bar_timestamp": "bar-2"},
        order_key="latency-order", last_processed_bar_timestamp="bar-1",
        bars_waited=0, latency_bars=2, queue_fill_probability=1.0,
        rejection_probability=0.0,
    )
    assert latency["event"] == "LATENCY_WAIT" and latency["fill_quantity"] == 0.0

    no_queue = simulate_paper_fill_step(
        remaining_quantity=10.0,
        context={**base, "bar_timestamp": "bar-3"},
        order_key="no-queue-order", last_processed_bar_timestamp="bar-2",
        bars_waited=0, latency_bars=1, queue_fill_probability=0.0,
        rejection_probability=0.0,
    )
    assert no_queue["event"] == "QUEUE_NO_FILL" and no_queue["fill_quantity"] == 0.0

    rejected = simulate_paper_fill_step(
        remaining_quantity=10.0,
        context={**base, "bar_timestamp": "bar-4"},
        order_key="rejected-order", last_processed_bar_timestamp="bar-3",
        bars_waited=0, latency_bars=1, queue_fill_probability=1.0,
        rejection_probability=1.0,
    )
    assert rejected["event"] == "REJECTED"
    assert rejected["remaining_quantity"] == 0.0

    unverified = simulate_paper_fill_step(
        remaining_quantity=10.0,
        context={**base, "source": "oanda_v20_mid", "asset": "forex",
                 "volume_unit": "tick_count", "bar_timestamp": "bar-5"},
        order_key="unverified-order", last_processed_bar_timestamp="bar-4",
        bars_waited=0, latency_bars=1, queue_fill_probability=1.0,
        rejection_probability=0.0,
    )
    assert unverified["event"] == "WAITING_FOR_VERIFIED_LIQUIDITY"
    assert unverified["fill_quantity"] == 0.0


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
