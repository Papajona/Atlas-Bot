"""Runtime regression coverage for direct execution of the AI model checker."""
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_ai_models.py"


def test_ai_model_checker_runs_from_outside_repository_without_provider_keys(tmp_path):
    """Exercise the real CLI import path without network access or credentials."""
    env = os.environ.copy()
    # Prevent a developer/CI environment from causing real provider requests.
    # Removing PYTHONPATH ensures it cannot mask a missing root-path bootstrap.
    env.pop("GEMINI_API_KEY", None)
    env.pop("GROQ_API_KEY", None)
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "AI provider verification: NOT VERIFIED (missing GEMINI_API_KEY, GROQ_API_KEY)." in result.stdout
    assert "ModuleNotFoundError" not in result.stderr
