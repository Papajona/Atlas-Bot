import pytest

from app.paper_execution_model import (
    PaperExecutionModelError,
    calibrated_model_available,
    estimate_paper_fill,
)


def _context(**overrides):
    return {
        "tradable_volume_units": 100.0,
        "spread_bps": 4.0,
        "recent_volatility_bps": 200.0,
        "bar_duration_ms": 3_600_000,
        **overrides,
    }


def _calibration(**overrides):
    return {
        "max_participation_rate": 0.10,
        "impact_coefficient": 0.5,
        "rejection_probability": 0.0,
        "latency_min_ms": 10.0,
        "latency_median_ms": 20.0,
        "latency_p95_ms": 50.0,
        "latency_max_ms": 100.0,
        "latency_impact_coefficient": 0.5,
        "evidence_source": "venue-paper-fill-study",
        "evidence_id": "sha256:calibration-fixture",
        **overrides,
    }


def _estimate(quantity, *, side="buy", order_key="order-0001", context=None, calibration=None):
    return estimate_paper_fill(
        order_key=order_key,
        side=side,
        requested_quantity=quantity,
        reference_price=100.0,
        base_slippage_bps=2.0,
        market_context=_context(**(context or {})),
        **_calibration(**(calibration or {})),
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
    assert result["evidence_id"] == "sha256:calibration-fixture"


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
    ],
)
def test_invalid_or_uncalibrated_model_inputs_fail_closed(context, calibration):
    inputs = _calibration(**calibration)
    assert calibrated_model_available(
        context,
        max_participation_rate=inputs["max_participation_rate"],
        impact_coefficient=inputs["impact_coefficient"],
        rejection_probability=inputs["rejection_probability"],
        latency_min_ms=inputs["latency_min_ms"],
        latency_median_ms=inputs["latency_median_ms"],
        latency_p95_ms=inputs["latency_p95_ms"],
        latency_max_ms=inputs["latency_max_ms"],
        latency_impact_coefficient=inputs["latency_impact_coefficient"],
        evidence_source=inputs["evidence_source"],
        evidence_id=inputs["evidence_id"],
    ) is False
    with pytest.raises(PaperExecutionModelError):
        estimate_paper_fill(
            order_key="invalid-input",
            side="buy",
            requested_quantity=1.0,
            reference_price=100.0,
            base_slippage_bps=2.0,
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
        estimate_paper_fill(
            order_key="",
            side="buy",
            requested_quantity=1.0,
            reference_price=100.0,
            base_slippage_bps=2.0,
            market_context=_context(),
            **_calibration(),
        )


def test_calibrated_model_availability_requires_every_latency_and_rejection_setting():
    calibration = _calibration()
    del calibration["rejection_probability"]
    assert calibrated_model_available(
        _context(),
        max_participation_rate=0.1,
        impact_coefficient=0.5,
        rejection_probability=None,
        latency_min_ms=10,
        latency_median_ms=20,
        latency_p95_ms=50,
        latency_max_ms=100,
        latency_impact_coefficient=0.5,
        evidence_source="venue-paper-fill-study",
        evidence_id="sha256:calibration-fixture",
    ) is False
