"""Behavioral tests for the deployment-time TRON funding safety gate."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "deploy" / "tron-funding-gate.sh"


def _run_gate(overrides: dict[str, str] | None = None, *, remove: tuple[str, ...] = ()):
    env = os.environ.copy()
    for key in ("USDT_TRON_ENABLED", "USDT_TRON_NETWORK", "ALLOW_TRON_FUNDING", "ALLOW_MAINNET_TRON_FUNDING", *remove):
        env.pop(key, None)
    env.update(overrides or {})
    return subprocess.run(
        [
            "bash", "-c",
            'source "$1"; atlas_configure_tron_funding; printf "%s|%s" "$USDT_TRON_ENABLED" "$USDT_TRON_NETWORK"',
            "_", str(GATE),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_tron_funding_defaults_off():
    result = _run_gate()
    assert result.returncode == 0, result.stderr
    assert result.stdout == "false|mainnet"


def test_tron_funding_requires_explicit_approval():
    result = _run_gate({"USDT_TRON_ENABLED": "true"})
    assert result.returncode != 0
    assert "ALLOW_TRON_FUNDING=YES" in result.stderr


def test_mainnet_tron_funding_requires_second_explicit_approval():
    result = _run_gate({"USDT_TRON_ENABLED": "true", "ALLOW_TRON_FUNDING": "YES"})
    assert result.returncode != 0
    assert "ALLOW_MAINNET_TRON_FUNDING=YES" in result.stderr


def test_approved_mainnet_tron_funding_is_explicit_and_configured():
    result = _run_gate({
        "USDT_TRON_ENABLED": "true",
        "USDT_TRON_NETWORK": "mainnet",
        "ALLOW_TRON_FUNDING": "YES",
        "ALLOW_MAINNET_TRON_FUNDING": "YES",
    })
    assert result.returncode == 0, result.stderr
    assert result.stdout == "true|mainnet"


def test_invalid_tron_funding_boolean_fails_closed():
    result = _run_gate({"USDT_TRON_ENABLED": "1"})
    assert result.returncode != 0
    assert "must be exactly true or false" in result.stderr
