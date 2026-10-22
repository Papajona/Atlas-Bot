"""Regression tests for research environment validation on both Cloud Run deploy paths."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "deploy" / "validate_research_deploy_env.py"


def validate(source: str, evidence_id: str, threshold: str, delimiter: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), source, evidence_id, threshold, delimiter],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("delimiter", [",", "@"])
def test_research_deploy_settings_accept_valid_values(delimiter: str) -> None:
    result = validate("venue-fee-schedule", "sha256-evidence-123", "0.95", delimiter)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("threshold", ["0.949", "nan", "NaN", "inf", "-inf", "not-a-number", ""])
def test_research_deploy_settings_reject_invalid_thresholds(threshold: str) -> None:
    result = validate("venue-fee-schedule", "sha256-evidence-123", threshold, ",")
    assert result.returncode == 2
    assert "RESEARCH_MIN_DEFLATED_SHARPE" in result.stderr


@pytest.mark.parametrize(
    "source,evidence_id",
    [("", "evidence"), ("   ", "evidence"), ("source", ""), ("source", "  ")],
)
def test_research_deploy_settings_reject_empty_evidence_fields(source: str, evidence_id: str) -> None:
    result = validate(source, evidence_id, "0.95", ",")
    assert result.returncode == 2


@pytest.mark.parametrize(
    "source,evidence_id,threshold,delimiter",
    [
        ("fee,provider", "evidence", "0.95", ","),
        ("fee", "evidence,123", "0.95", ","),
        ("fee", "evidence", "0,95", ","),
        ("fee@provider", "evidence", "0.95", "@"),
        ("fee", "evidence@123", "0.95", "@"),
        ("fee", "evidence", "0.95@unexpected", "@"),
    ],
)
def test_research_deploy_settings_reject_active_delimiter(
    source: str, evidence_id: str, threshold: str, delimiter: str
) -> None:
    result = validate(source, evidence_id, threshold, delimiter)
    assert result.returncode == 2
    assert "delimiter" in result.stderr.lower() or "RESEARCH_MIN_DEFLATED_SHARPE" in result.stderr


def test_both_production_deploy_scripts_call_shared_research_validator() -> None:
    api = (ROOT / "deploy" / "cloud-run-deploy.sh").read_text(encoding="utf-8")
    worker = (ROOT / "deploy" / "cloud-run-worker-deploy.sh").read_text(encoding="utf-8")

    assert "validate_research_deploy_env.py" in api
    assert ' "$RESEARCH_FEE_SOURCE" "$RESEARCH_FEE_EVIDENCE_ID" "$RESEARCH_MIN_DEFLATED_SHARPE" "@"' in api
    assert "validate_research_deploy_env.py" in worker
    # Validation alone is insufficient: these values must reach the deployed API and worker runtime.
    assert "@RESEARCH_FEE_SOURCE=${RESEARCH_FEE_SOURCE}@RESEARCH_FEE_EVIDENCE_ID=${RESEARCH_FEE_EVIDENCE_ID}@RESEARCH_MIN_DEFLATED_SHARPE=${RESEARCH_MIN_DEFLATED_SHARPE}" in api
    assert "RESEARCH_FEE_SOURCE=${RESEARCH_FEE_SOURCE},RESEARCH_FEE_EVIDENCE_ID=${RESEARCH_FEE_EVIDENCE_ID},RESEARCH_MIN_DEFLATED_SHARPE=${RESEARCH_MIN_DEFLATED_SHARPE}" in worker
    assert ' "$RESEARCH_FEE_SOURCE" "$RESEARCH_FEE_EVIDENCE_ID" "$RESEARCH_MIN_DEFLATED_SHARPE" ","' in worker
    dollar = chr(36)
    for script in (api, worker):
        for name in ("RESEARCH_FEE_SOURCE", "RESEARCH_FEE_EVIDENCE_ID", "RESEARCH_MIN_DEFLATED_SHARPE"):
            assert f': "{dollar}{{{name}:?' in script
