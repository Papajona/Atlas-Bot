import hashlib
from pathlib import Path

import pytest

from app.paper_execution_model import (
    PaperExecutionModelError,
    calibrated_model_available,
    estimate_paper_fill,
)

TEST_EVIDENCE_PATH = Path(__file__).parent / "fixtures" / "paper_execution_calibration_test.json"
TEST_EVIDENCE_ID = "sha256:" + hashlib.sha256(TEST_EVIDENCE_PATH.read_bytes()).hexdigest()


def _context(**overrides):
    return {
        "tradable_volume_units": 100.0,
        "spread_bps": 4.0,
        "recent_volatility_bps": 200.0,
        "bar_duration_ms": 3_600_000,
        "exchange": "fakeexchange",
        "symbol": "BTC/USDT",
        "market_type": "spot",
        **overrides,
    }


def _calibration(**overrides):
    return {
        "base_slippage_bps": 2.0,
        "max_participation_rate": 0.10,
        "impact_coefficient": 0.5,
        "rejection_probability": 0.0,
        "latency_min_ms": 10.0,
        "latency_median_ms": 20.0,
        "latency_p95_ms": 50.0,
        "latency_max_ms": 100.0,
        "latency_impact_coefficient": 0.5,
        "evidence_source": "venue-paper-fill-study",
        "evidence_path": str(TEST_EVIDENCE_PATH),
        "evidence_id": TEST_EVIDENCE_ID,
        "allow_test_fixture": True,
        **overrides,
    }


def _estimate(quantity, *, side="buy", order_key="order-0001", context=None, calibration=None, allow_simulated_rejection=True):
    inputs = _calibration(**(calibration or {}))
    base_slippage_bps = inputs.pop("base_slippage_bps")
    return estimate_paper_fill(
        order_key=order_key,
        side=side,
        requested_quantity=quantity,
        reference_price=100.0,
        base_slippage_bps=base_slippage_bps,
        market_context=_context(**(context or {})),
        allow_simulated_rejection=allow_simulated_rejection,
        **inputs,
    )


def test_impact_increases_with_order_participation_and_volatility():
    small = _estimate(1.0)
    larger = _estimate(4.0)
    higher_volatility = _estimate(1.0, context={"recent_volatility_bps": 400.0})

    assert larger["realized_participation_rate"] > small["realized_participation_rate"]
    assert larger["market_impact_bps"] > small["market_impact_bps"]
    assert higher_volatility["market_impact_bps"] > small["market_impact_bps"]
    assert larger["fill_price"] > small["fill_price"] > 100.0
    assert "queue_position_not_modelled" in result_limitations(larger)


def result_limitations(result):
    return result["model_limitations"]


def test_volume_cap_creates_explicit_partial_fill_and_cancels_remainder():
    result = _estimate(20.0)

    assert result["filled_quantity"] == 10.0
    assert result["cancelled_quantity"] == 10.0
    assert result["full_fill"] is False
    assert result["simulated_rejection"] is False
    assert result["validation_grade"] == "INCOMPLETE_EXECUTION_MODEL"
    assert "queue_position_not_modelled" in result["model_limitations"]
    assert "latency_price_adjustment_uses_calibrated_coefficient_and_sqrt_time_approximation" in result["model_limitations"]


def test_sell_slippage_is_adverse_and_model_evidence_is_mandatory():
    result = _estimate(1.0, side="sell")
    assert result["fill_price"] < 100.0
    assert result["evidence_source"] == "venue-paper-fill-study"
    assert result["evidence_id"] == TEST_EVIDENCE_ID


def test_latency_changes_adverse_cost_and_order_key_makes_result_reproducible():
    low = _estimate(1.0, order_key="stable-order", calibration={"latency_impact_coefficient": 0.0})
    high = _estimate(1.0, order_key="stable-order", calibration={"latency_impact_coefficient": 2.0})
    repeated = _estimate(1.0, order_key="stable-order", calibration={"latency_impact_coefficient": 2.0})

    assert high["simulated_latency_ms"] == repeated["simulated_latency_ms"]
    assert high["latency_quantile_draw"] == repeated["latency_quantile_draw"]
    assert high["latency_impact_bps"] > low["latency_impact_bps"]
    assert high["fill_price"] > low["fill_price"] > 100.0


def test_calibrated_venue_rejection_has_no_fill_and_cancels_entire_order():
    result = _estimate(
        5.0,
        order_key="always-reject",
        calibration={"rejection_probability": 1.0},
    )

    assert result["model_status"] == "SIMULATED_VENUE_REJECTION"
    assert result["simulated_rejection"] is True
    assert result["filled_quantity"] == 0.0
    assert result["cancelled_quantity"] == 5.0
    assert result["fill_price"] is None
    assert result["total_slippage_bps"] is None


@pytest.mark.parametrize(
    "context,calibration",
    [
        (_context(tradable_volume_units=0), {}),
        (_context(recent_volatility_bps=float("nan")), {}),
        (_context(), {"max_participation_rate": 1.1}),
        (_context(), {"impact_coefficient": -0.1}),
        (_context(), {"rejection_probability": -0.1}),
        (_context(), {"rejection_probability": 1.1}),
        (_context(), {"latency_min_ms": 30, "latency_median_ms": 20}),
        (_context(), {"latency_median_ms": 60, "latency_p95_ms": 50}),
        (_context(), {"latency_impact_coefficient": -0.1}),
        (_context(), {"evidence_source": ""}),
        (_context(), {"evidence_id": ""}),
        (_context(), {"evidence_id": "not-a-sha256-digest"}),
    ],
)
def test_invalid_or_uncalibrated_model_inputs_fail_closed(context, calibration):
    inputs = _calibration(**calibration)
    assert calibrated_model_available(
        context,
        base_slippage_bps=inputs["base_slippage_bps"],
        max_participation_rate=inputs["max_participation_rate"],
        impact_coefficient=inputs["impact_coefficient"],
        rejection_probability=inputs["rejection_probability"],
        latency_min_ms=inputs["latency_min_ms"],
        latency_median_ms=inputs["latency_median_ms"],
        latency_p95_ms=inputs["latency_p95_ms"],
        latency_max_ms=inputs["latency_max_ms"],
        latency_impact_coefficient=inputs["latency_impact_coefficient"],
        evidence_source=inputs["evidence_source"],
        evidence_path=inputs["evidence_path"],
        evidence_id=inputs["evidence_id"],
    ) is False
    with pytest.raises(PaperExecutionModelError):
        base_slippage_bps = inputs.pop("base_slippage_bps")
        estimate_paper_fill(
            order_key="invalid-input",
            side="buy",
            requested_quantity=1.0,
            reference_price=100.0,
            base_slippage_bps=base_slippage_bps,
            market_context=context,
            **inputs,
        )


def test_zero_volume_cannot_be_treated_as_a_full_fill():
    with pytest.raises(PaperExecutionModelError, match="tradable_volume_units"):
        _estimate(1.0, context={"tradable_volume_units": 0})


def test_missing_bar_duration_and_order_key_are_rejected():
    with pytest.raises(PaperExecutionModelError, match="bar_duration_ms"):
        _estimate(1.0, context={"bar_duration_ms": None})
    with pytest.raises(PaperExecutionModelError, match="order_key"):
        inputs = _calibration()
        base_slippage_bps = inputs.pop("base_slippage_bps")
        estimate_paper_fill(
            order_key="",
            side="buy",
            requested_quantity=1.0,
            reference_price=100.0,
            base_slippage_bps=base_slippage_bps,
            market_context=_context(),
            **inputs,
        )


def test_calibrated_model_availability_requires_every_latency_and_rejection_setting():
    assert calibrated_model_available(
        _context(),
        base_slippage_bps=2.0,
        max_participation_rate=0.1,
        impact_coefficient=0.5,
        rejection_probability=None,
        latency_min_ms=10,
        latency_median_ms=20,
        latency_p95_ms=50,
        latency_max_ms=100,
        latency_impact_coefficient=0.5,
        evidence_source="venue-paper-fill-study",
        evidence_path=str(TEST_EVIDENCE_PATH),
        evidence_id=TEST_EVIDENCE_ID,
        allow_test_fixture=True,
    ) is False


def test_test_fixture_is_accepted_only_with_explicit_development_override():
    calibration = _calibration()
    shared = {
        "base_slippage_bps": calibration["base_slippage_bps"],
        "max_participation_rate": calibration["max_participation_rate"],
        "impact_coefficient": calibration["impact_coefficient"],
        "rejection_probability": calibration["rejection_probability"],
        "latency_min_ms": calibration["latency_min_ms"],
        "latency_median_ms": calibration["latency_median_ms"],
        "latency_p95_ms": calibration["latency_p95_ms"],
        "latency_max_ms": calibration["latency_max_ms"],
        "latency_impact_coefficient": calibration["latency_impact_coefficient"],
        "evidence_source": calibration["evidence_source"],
        "evidence_path": calibration["evidence_path"],
        "evidence_id": calibration["evidence_id"],
    }
    assert calibrated_model_available(_context(), **shared) is False
    assert calibrated_model_available(
        _context(), **shared, allow_test_fixture=True
    ) is True


def test_numeric_model_settings_cannot_drift_from_sha_pinned_calibration_parameters():
    with pytest.raises(PaperExecutionModelError, match="calibration artifact"):
        _estimate(1.0, calibration={"impact_coefficient": 0.7})



def test_runtime_configuration_is_all_or_nothing_and_never_allows_test_fixtures():
    from types import SimpleNamespace
    from app.paper_execution_model import configured_paper_execution_settings

    empty = SimpleNamespace(**{
        "paper_impact_max_participation_rate": None,
        "paper_impact_coefficient": None,
        "paper_execution_rejection_probability": None,
        "paper_execution_latency_min_ms": None,
        "paper_execution_latency_median_ms": None,
        "paper_execution_latency_p95_ms": None,
        "paper_execution_latency_max_ms": None,
        "paper_latency_impact_coefficient": None,
        "paper_execution_evidence_source": "",
        "paper_execution_evidence_path": "",
        "paper_execution_evidence_id": "",
        "research_slippage_bps": 2.0,
    })
    assert configured_paper_execution_settings(empty) is None

    partial = SimpleNamespace(**{
        **vars(empty),
        "paper_impact_max_participation_rate": 0.1,
    })
    with pytest.raises(PaperExecutionModelError, match="configuration is incomplete"):
        configured_paper_execution_settings(partial)

    configured = SimpleNamespace(**{
        **vars(empty),
        "paper_impact_max_participation_rate": 0.1,
        "paper_impact_coefficient": 0.5,
        "paper_execution_rejection_probability": 0.0,
        "paper_execution_latency_min_ms": 10.0,
        "paper_execution_latency_median_ms": 20.0,
        "paper_execution_latency_p95_ms": 50.0,
        "paper_execution_latency_max_ms": 100.0,
        "paper_latency_impact_coefficient": 0.5,
        "paper_execution_evidence_source": "venue-study",
        "paper_execution_evidence_path": "/evidence/calibration.json",
        "paper_execution_evidence_id": "a" * 64,
    })
    result = configured_paper_execution_settings(configured)
    assert result["base_slippage_bps"] == 2.0
    assert result["max_participation_rate"] == 0.1
    assert result["allow_test_fixture"] is False


def test_calibrated_fill_respects_the_remaining_shared_bar_capacity():
    result = estimate_paper_fill(
        order_key="shared-capacity-order",
        side="buy",
        requested_quantity=8.0,
        reference_price=100.0,
        market_context=_context(),
        available_capacity_units=4.0,
        **{
            key: value for key, value in _calibration().items()
            if key not in {"allow_test_fixture"}
        },
        allow_test_fixture=True,
    )
    assert result["filled_quantity"] == 4.0
    assert result["cancelled_quantity"] == 4.0
    assert result["full_fill"] is False
    assert result["available_capacity_units"] == 4.0


def test_protective_exit_suppresses_rejection_but_retains_calibrated_probability(monkeypatch):
    monkeypatch.setattr(
        "app.paper_execution_model.deterministic_execution_draws",
        lambda order_key: (0.0, 0.5),
    )
    rejected = _estimate(
        1.0,
        order_key="same-protective-exit-draw",
        calibration={"rejection_probability": 1.0},
    )
    protected = _estimate(
        1.0,
        order_key="same-protective-exit-draw",
        calibration={"rejection_probability": 1.0},
        allow_simulated_rejection=False,
    )
    assert rejected["simulated_rejection"] is True
    assert rejected["rejection_applied"] is True
    assert protected["simulated_rejection"] is False
    assert protected["rejection_applied"] is False
    assert protected["rejection_probability"] == 1.0
    assert protected["filled_quantity"] > 0
