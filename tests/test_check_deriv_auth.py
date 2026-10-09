"""Regression tests for safe Deriv token verification configuration handling."""
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_deriv_auth.py"


def test_deriv_checker_fails_closed_without_credentials_from_outside_repository(tmp_path):
    env = os.environ.copy()
    env.pop("DERIV_APP_ID", None)
    env.pop("DERIV_API_TOKEN", None)
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "Deriv authentication: NOT VERIFIED (missing DERIV_APP_ID, DERIV_API_TOKEN)." in result.stdout
    assert "Traceback" not in result.stderr
