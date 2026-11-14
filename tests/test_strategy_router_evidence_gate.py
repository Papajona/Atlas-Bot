from __future__ import annotations

from app.main import _strategy_router_validation_gate
from app.schemas import CustomerBotStartRequest


def _verified_validation() -> dict:
    return {
        "status": "OK",
        "total_return": 0.12,
        "folds": [
            {"total_return": 0.04, "execution_cost_model_complete": True},
            {"total_return": 0.03, "execution_cost_model_complete": True},
            {"total_return": 0.05, "execution_cost_model_complete": True},
        ],
        "execution_cost_model_complete": True,
        "execution_cost_model_status": "VERIFIED_PER_BAR_SPREAD_VOLUME_PARTICIPATION_LATENCY",
        "execution_cost_calibration_status": "VERIFIED",
        "execution_cost_calibration_verified": True,
        "execution_cost_calibration_evidence_id": "signed-venue-calibration-2026-001",
        "execution_cost_model_fingerprint": "a" * 64,
        "per_bar_execution_context_sha256": "b" * 64,
        "execution_cost_dataset_sha256": "c" * 64,
    }


def _verified_context() -> dict:
    return {
        "execution_cost_calibration_status": "VERIFIED",
        "execution_cost_calibration_evidence_id": "signed-venue-calibration-2026-001",
        "evidence_id": "signed-venue-calibration-2026-001",
        "model_fingerprint": "a" * 64,
        "context_sha256": "b" * 64,
        "dataset_sha256": "c" * 64,
    }


def _gate(validation=None, context=None, dataset_sha256="c" * 64):
    return _strategy_router_validation_gate(
        validation or _verified_validation(),
        required_folds=3,
        required_positive_fold_ratio=0.5,
        current_dataset_sha256=dataset_sha256,
        current_execution_cost_context=context if context is not None else _verified_context(),
    )


def test_adaptive_router_promotion_requires_current_signed_per_bar_cost_context():
    result = _gate()
    assert result["passed"] is True
    assert result["execution_cost_calibration_verified"] is True
    assert result["execution_cost_model_complete"] is True
    assert result["execution_cost_dataset_matches_current_data"] is True


def test_adaptive_router_promotion_fails_without_signed_context():
    result = _strategy_router_validation_gate(
        _verified_validation(),
        required_folds=3,
        required_positive_fold_ratio=0.5,
        current_dataset_sha256="c" * 64,
        current_execution_cost_context=None,
    )
    assert result["passed"] is False
    assert "SIGNED_VENUE_CALIBRATION_EVIDENCE_NOT_VERIFIED" in result["rejection_reasons"]


def test_adaptive_router_promotion_rejects_changed_evidence_and_model_fingerprints():
    context = _verified_context()
    context["execution_cost_calibration_evidence_id"] = "different-signed-document"
    result = _gate(context=context)
    assert result["passed"] is False
    assert result["execution_cost_calibration_verified"] is False

    context = _verified_context()
    context["model_fingerprint"] = "d" * 64
    result = _gate(context=context)
    assert result["passed"] is False
    assert result["execution_cost_model_complete"] is False


def test_adaptive_router_promotion_rejects_dataset_mismatch_and_incomplete_fold():
    result = _gate(dataset_sha256="d" * 64)
    assert result["passed"] is False
    assert result["execution_cost_dataset_matches_current_data"] is False

    validation = _verified_validation()
    validation["folds"][1]["execution_cost_model_complete"] = False
    result = _gate(validation=validation)
    assert result["passed"] is False
    assert result["execution_cost_model_complete"] is False


def test_customer_bot_request_accepts_bounded_signed_execution_evidence():
    evidence = {"evidence_type": "venue_execution_cost_calibration", "attestation": {"key_id": "trusted"}}
    request = CustomerBotStartRequest(execution_cost_calibration_evidence=evidence)
    assert request.execution_cost_calibration_evidence == evidence


def test_customer_bot_request_rejects_oversized_or_nonfinite_execution_evidence():
    from pydantic import ValidationError
    import pytest

    with pytest.raises(ValidationError):
        CustomerBotStartRequest(execution_cost_calibration_evidence={"payload": "x" * (512 * 1024)})

    with pytest.raises(ValidationError):
        CustomerBotStartRequest(execution_cost_calibration_evidence={"nonfinite": float("nan")})
