"""Runtime regression coverage for direct execution of the AI model checker."""
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_ai_models.py"


def _run_without_network_keys(tmp_path, *, gemini=None, groq=None):
    env = os.environ.copy()
    env.pop("GEMINI_API_KEY", None)
    env.pop("GROQ_API_KEY", None)
    env.pop("PYTHONPATH", None)
    if gemini is not None:
        env["GEMINI_API_KEY"] = gemini
    if groq is not None:
        env["GROQ_API_KEY"] = groq
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_ai_model_checker_runs_from_outside_repository_without_provider_keys(tmp_path):
    """Exercise the real CLI import path without network access or credentials."""
    result = _run_without_network_keys(tmp_path)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "AI provider verification: NOT VERIFIED (missing GEMINI_API_KEY, GROQ_API_KEY)." in result.stdout
    assert "ModuleNotFoundError" not in result.stderr


def test_ai_model_checker_fails_closed_if_only_one_provider_key_is_set(tmp_path):
    """A single configured provider must not make the two-provider gate pass."""
    result = _run_without_network_keys(tmp_path, gemini="test-placeholder-not-a-real-key")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "AI provider verification: NOT VERIFIED (missing GROQ_API_KEY)." in result.stdout
    assert "generation failed" not in result.stdout
