from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_groq_deploy_env.py"


@pytest.mark.parametrize("enabled", ["true", "false", "TRUE", "False"])
def test_groq_deployment_validator_accepts_toggle_and_safe_values(enabled: str) -> None:
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "openai/gpt-oss-20b", "2048", enabled],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "values",
    [
        ("openai/model@extra", "2048", "true"),
        ("openai/model", "0", "true"),
        ("openai/model", "8193", "true"),
        ("openai/model", "2048", "enabled"),
        ("openai/model", "2048,ENVIRONMENT=dev", "false"),
    ],
)
def test_groq_deployment_validator_rejects_unsafe_values(values: tuple[str, str, str]) -> None:
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), *values],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "ERROR:" in result.stderr


def test_api_defaults_groq_and_worker_derives_same_values_from_api_revision() -> None:
    api = (ROOT / "deploy" / "cloud-run-deploy.sh").read_text(encoding="utf-8")
    worker = (ROOT / "deploy" / "cloud-run-worker-deploy.sh").read_text(encoding="utf-8")
    assert 'GROQ_MODEL="${GROQ_MODEL:-openai/gpt-oss-20b}"' in api
    assert 'GROQ_MAX_COMPLETION_TOKENS="${GROQ_MAX_COMPLETION_TOKENS:-2048}"' in api
    assert 'GROQ_RESEARCH_REVIEW_ENABLED="${GROQ_RESEARCH_REVIEW_ENABLED:-true}"' in api
    assert 'GROQ_MODEL="${GROQ_MODEL:-openai/gpt-oss-20b}"' not in worker
    assert 'GROQ_MAX_COMPLETION_TOKENS="${GROQ_MAX_COMPLETION_TOKENS:-2048}"' not in worker
    assert 'GROQ_RESEARCH_REVIEW_ENABLED="${GROQ_RESEARCH_REVIEW_ENABLED:-true}"' not in worker
    assert ': "${API_SERVICE:?Set API_SERVICE to the currently deployed Cloud Run API service}"' in worker
    assert 'gcloud run services describe "$API_SERVICE"' in worker
    assert 'scripts/resolve_worker_groq_config.py' in worker
    assert 'GROQ_MODEL="${API_GROQ_CONFIG[0]}"' in worker
    assert 'GROQ_MAX_COMPLETION_TOKENS="${API_GROQ_CONFIG[1]}"' in worker
    assert 'GROQ_RESEARCH_REVIEW_ENABLED="${API_GROQ_CONFIG[2]}"' in worker
    assert 'SECRET_GROQ_REF="${API_GROQ_CONFIG[3]}"' in worker
    assert "supplied GROQ_MODEL differs from the deployed API" in worker
    assert "supplied GROQ_MAX_COMPLETION_TOKENS differs from the deployed API" in worker
    assert "supplied GROQ_RESEARCH_REVIEW_ENABLED differs from the deployed API" in worker
    assert ": \"${GROQ_MODEL:?" not in worker
    assert ": \"${GROQ_MAX_COMPLETION_TOKENS:?" not in worker
    assert ": \"${GROQ_RESEARCH_REVIEW_ENABLED:?" not in worker
    assert "validate_groq_deploy_env.py" in api
    assert "validate_groq_deploy_env.py" in worker
