import hashlib
from pathlib import Path

import pandas as pd
import pytest

from app.execution_costs import (
    EXECUTION_COST_MODEL_VERSION,
    adverse_fill_price,
    execution_cost_components,
    net_return_after_costs,
)
from app.execution_aware_research import costed_execution_returns


def test_shared_cost_kernel_matches_paper_fill_components():
    from app.paper_execution_model import estimate_paper_fill

    evidence_path = Path(__file__).parent / "fixtures" / "paper_execution_calibration_test.json"
    evidence_id = "sha256:" + hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    context = {
        "tradable_volume_units": 100.0,
        "spread_bps": 4.0,
        "recent_volatility_bps": 200.0,
        "bar_duration_ms": 3_600_000,
        "exchange": "fakeexchange",
        "symbol": "BTC/USDT",
        "market_type": "spot",
    }
    fill = estimate_paper_fill(
        order_key="shared-execution-cost-parity",
        side="buy",
        requested_quantity=4.0,
        reference_price=100.0,
        base_slippage_bps=2.0,
        market_context=context,
        max_participation_rate=0.10,
        impact_coefficient=0.5,
        rejection_probability=0.0,
        latency_min_ms=10.0,
        latency_median_ms=20.0,
        latency_p95_ms=50.0,
        latency_max_ms=100.0,
        latency_impact_coefficient=0.5,
        evidence_source="venue-paper-fill-study",
        evidence_path=str(evidence_path),
        evidence_id=evidence_id,
        allow_test_fixture=True,
    )
    expected = execution_cost_components(
        base_slippage_bps=2.0,
        spread_bps=4.0,
        volatility_bps=200.0,
        participation_rate=fill["realized_participation_rate"],
        impact_coefficient=0.5,
        latency_ms=fill["simulated_latency_ms"],
        bar_duration_ms=3_600_000,
        latency_impact_coefficient=0.5,
    )

    assert fill["execution_cost_model_version"] == EXECUTION_COST_MODEL_VERSION
    assert fill["market_impact_bps"] == pytest.approx(expected["market_impact_bps"])
    assert fill["latency_impact_bps"] == pytest.approx(expected["latency_impact_bps"])
    assert fill["total_slippage_bps"] == pytest.approx(expected["total_slippage_bps"])
    assert fill["fill_price"] == pytest.approx(
        adverse_fill_price(100.0, "buy", expected["total_slippage_bps"])
    )


def test_router_and_research_apply_identical_per_side_net_return_arithmetic(monkeypatch):
    from app import strategy_router

    index = pd.date_range("2026-01-01T00:00:00Z", periods=2, freq="h")
    frame = pd.DataFrame(
        {"open": [100.0, 101.0], "high": [101.0, 102.0],
         "low": [99.0, 100.0], "close": [100.0, 101.0]},
        index=index,
    )
    requested = pd.Series([0.5, 0.5], index=index)
    monkeypatch.setattr(
        strategy_router,
        "strategy_signals",
        lambda _df, _cfg, asset="crypto": pd.DataFrame(
            {"momentum": requested, "realized_vol": pd.Series(0.01, index=index)},
            index=index,
        ),
    )
    monkeypatch.setattr(
        strategy_router,
        "target_positions",
        lambda signal, _vol, _cfg: pd.Series([0.5, 0.5], index=index),
    )
    monkeypatch.setattr(
        strategy_router,
        "next_open_returns",
        lambda _df: pd.Series([1.0, 1.01], index=index),
    )

    router_returns = strategy_router._strategy_returns(
        frame, object(), "momentum", costs_bps=10.0, asset="crypto"
    )
    positions = pd.Series([0.0, 0.5], index=index)
    market_returns = pd.Series([0.0, 0.01], index=index)
    research_returns = costed_execution_returns(
        positions,
        market_returns,
        transaction_cost_bps=10.0,
        carry_bps_per_bar=0.0,
    )

    assert router_returns.to_list() == pytest.approx(research_returns.to_list())
    assert router_returns.iloc[1] == pytest.approx(0.0045)


def test_research_returns_and_triple_barrier_use_the_shared_net_return_kernel():
    from app.trading_core import triple_barrier

    gross_return = 0.012
    cost_bps = 15.0
    expected = net_return_after_costs(
        gross_return, turnover=1.0, execution_cost_bps=cost_bps
    )

    index = pd.date_range("2026-01-01T00:00:00Z", periods=1, freq="h")
    result = costed_execution_returns(
        pd.Series([1.0], index=index),
        pd.Series([gross_return], index=index),
        transaction_cost_bps=cost_bps,
        carry_bps_per_bar=0.0,
    )
    assert result.iloc[0] == pytest.approx(expected)

    label_index = pd.date_range("2026-01-01T00:00:00Z", periods=4, freq="h")
    close = pd.Series([120.0, 100.0, 101.9, 103.0], index=label_index)
    vol = pd.Series([0.01] * 4, index=label_index)
    high = pd.Series([121.0, 101.5, 102.9, 104.0], index=label_index)
    low = pd.Series([119.0, 99.5, 100.0, 101.0], index=label_index)
    opens = pd.Series([120.0, 100.0, 102.0, 103.0], index=label_index)
    net_label = triple_barrier(
        close, vol, high, low, pt=1.0, sl=1.0, max_hold=2,
        entry_open=opens, round_trip_cost_bps=200.0,
    )
    assert net_label.iloc[0] == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_slippage_bps": -1.0, "spread_bps": 1, "volatility_bps": 1,
         "participation_rate": 0.1, "impact_coefficient": 0.5},
        {"base_slippage_bps": 1.0, "spread_bps": 1, "volatility_bps": 1,
         "participation_rate": 1.1, "impact_coefficient": 0.5},
        {"base_slippage_bps": 1.0, "spread_bps": 1, "volatility_bps": 1,
         "participation_rate": 0.1, "impact_coefficient": float("nan")},
    ],
)
def test_shared_execution_cost_kernel_rejects_invalid_market_inputs(kwargs):
    with pytest.raises(ValueError):
        execution_cost_components(**kwargs)



def test_market_impact_kernel_rejects_infinite_coefficient_before_impact_cap():
    from app.execution_costs import market_impact_bps

    # A finite impact cap must not mask an invalid runtime coefficient. This
    # exercises the real shared kernel validator, not a mocked caller failure.
    with pytest.raises(
        ValueError, match="impact_coefficient must be finite and non-negative"
    ):
        market_impact_bps(
            volatility_bps=200.0,
            participation_rate=0.1,
            impact_coefficient=float("inf"),
            max_market_impact_bps=100.0,
        )


def test_shared_net_return_kernel_rejects_invalid_costs():
    with pytest.raises(ValueError):
        net_return_after_costs(
            0.01, turnover=1.0, execution_cost_bps=float("nan")
        )


def test_active_paper_and_multi_asset_paths_use_the_shared_impact_formula():
    from decimal import Decimal
    from pathlib import Path

    from app.multi_asset_paper import SlippageInputs, estimate_execution_price
    from app.execution_costs import market_impact_bps

    root = Path(__file__).resolve().parents[1]
    customer_paper = (root / "app" / "customer_paper_api.py").read_text(encoding="utf-8")
    platform_paper = (root / "app" / "execution.py").read_text(encoding="utf-8")
    assert "from .execution_costs import market_impact_bps" in customer_paper
    assert "market_impact_bps(" in customer_paper
    assert "from .execution_costs import market_impact_bps" in platform_paper
    assert "market_impact_bps(" in platform_paper
    assert 'outcome in {"INVALID_EXIT_CONTEXT", "INVALID_EXECUTION_COST_CONTEXT"}' in platform_paper

    shared = market_impact_bps(
        volatility_bps=200.0,
        participation_rate=0.04,
        impact_coefficient=0.5,
    )
    assert shared == pytest.approx(20.0)

    price, total_cost_bps = estimate_execution_price(
        SlippageInputs(
            mid_price=Decimal("100"),
            half_spread_bps=Decimal("2"),
            volatility_bps=Decimal("200"),
            order_quantity=Decimal("4"),
            visible_depth_quantity=Decimal("100"),
            impact_coefficient=Decimal("0.5"),
            adverse_gap_bps=Decimal("0"),
        ),
        side="buy",
    )
    assert total_cost_bps == Decimal("22.0")
    assert price == Decimal("100.2200")


def test_missing_timeout_close_leaves_label_undefined_without_aborting_batch():
    from app.trading_core import triple_barrier

    index = pd.date_range("2026-01-01T00:00:00Z", periods=4, freq="h")
    close = pd.Series([100.0, 100.0, float("nan"), 100.0], index=index)
    high = close.copy()
    low = close.copy()
    opens = pd.Series([100.0, 100.0, 100.0, 100.0], index=index)
    volatility = pd.Series([0.01, 0.01, 0.01, 0.01], index=index)

    labels = triple_barrier(
        close,
        volatility,
        high,
        low,
        max_hold=2,
        entry_open=opens,
        round_trip_cost_bps=0.0,
    )

    assert pd.isna(labels.iloc[0])
    assert len(labels) == len(close)


def test_scalar_net_return_path_does_not_call_vectorized_kernel(monkeypatch):
    import app.execution_costs as costs

    def fail_if_vectorized(*_args, **_kwargs):
        raise AssertionError("scalar label path must not allocate vector buffers")

    monkeypatch.setattr(costs, "net_returns_after_costs", fail_if_vectorized)
    result = costs.net_return_after_costs(
        0.01,
        turnover=1.5,
        execution_cost_bps=12.0,
        exposure=-0.5,
        financing_cost_bps_per_bar=2.0,
        cost_multiplier=2.0,
    )
    assert result == pytest.approx(0.0062)
