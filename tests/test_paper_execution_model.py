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
        **overrides,
    }


def _estimate(quantity, **overrides):
    return estimate_paper_fill(
        side="buy",
        requested_quantity=quantity,
        reference_price=100.0,
        base_slippage_bps=2.0,
        market_context=_context(**overrides.pop("context", {})),
        max_participation_rate=0.10,
        impact_coefficient=0.5,
        evidence_source="venue-paper-fill-study",
        evidence_id="sha256:calibration-fixture",
        **overrides,
    )


def test_impact_increases_with_order_participation_and_volatility():
    small = _estimate(1.0)
    larger = _estimate(4.0)
    higher_volatility = _estimate(1.0, context={"recent_volatility_bps": 400.0})

    assert larger["realized_participation_rate"] > small["realized_participation_rate"]
    assert larger["market_impact_bps"] > small["market_impact_bps"]
    assert higher_volatility["market_impact_bps"] > small["market_impact_bps"]
    assert larger["fill_price"] > small["fill_price"] > 100.0


def test_volume_cap_creates_explicit_partial_fill_and_cancels_remainder():
    result = _estimate(20.0)

    assert result["filled_quantity"] == 10.0
    assert result["cancelled_quantity"] == 10.0
    assert result["full_fill"] is False
    assert result["validation_grade"] == "INCOMPLETE_EXECUTION_MODEL"
    assert "latency_and_queue_position_not_modelled" in result["model_limitations"]
    assert "venue_rejection_probability_not_modelled" in result["model_limitations"]


def test_sell_slippage_is_adverse_and_model_evidence_is_mandatory():
    result = estimate_paper_fill(
        side="sell",
        requested_quantity=1.0,
        reference_price=100.0,
        base_slippage_bps=2.0,
        market_context=_context(),
        max_participation_rate=0.10,
        impact_coefficient=0.5,
        evidence_source="venue-paper-fill-study",
        evidence_id="sha256:calibration-fixture",
    )
    assert result["fill_price"] < 100.0


@pytest.mark.parametrize(
    "context,participation,coefficient,source,evidence",
    [
        (_context(tradable_volume_units=0), 0.1, 0.5, "source", "id"),
        (_context(recent_volatility_bps=float("nan")), 0.1, 0.5, "source", "id"),
        (_context(), 1.1, 0.5, "source", "id"),
        (_context(), 0.1, -0.1, "source", "id"),
        (_context(), 0.1, 0.5, "", "id"),
        (_context(), 0.1, 0.5, "source", ""),
    ],
)
def test_invalid_or_uncalibrated_model_inputs_fail_closed(
    context, participation, coefficient, source, evidence
):
    assert calibrated_model_available(
        context,
        max_participation_rate=participation,
        impact_coefficient=coefficient,
        evidence_source=source,
        evidence_id=evidence,
    ) is False
    with pytest.raises(PaperExecutionModelError):
        estimate_paper_fill(
            side="buy",
            requested_quantity=1.0,
            reference_price=100.0,
            base_slippage_bps=2.0,
            market_context=context,
            max_participation_rate=participation,
            impact_coefficient=coefficient,
            evidence_source=source,
            evidence_id=evidence,
        )


def test_zero_volume_cannot_be_treated_as_a_full_fill():
    with pytest.raises(PaperExecutionModelError, match="tradable_volume_units"):
        estimate_paper_fill(
            side="buy",
            requested_quantity=1.0,
            reference_price=100.0,
            base_slippage_bps=2.0,
            market_context=_context(tradable_volume_units=0),
            max_participation_rate=0.1,
            impact_coefficient=0.5,
            evidence_source="venue-paper-fill-study",
            evidence_id="sha256:calibration-fixture",
        )
