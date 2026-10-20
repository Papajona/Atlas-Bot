"""Regression tests for audit findings fixed in the October 2026 repair branch."""
import hashlib
import json
from types import SimpleNamespace

import pytest


def test_constant_time_text_comparison_handles_non_ascii_values():
    from app.main import _constant_time_text_equal
    assert _constant_time_text_equal("valid-signature", "valid-signature") is True
    assert _constant_time_text_equal("sig-nature", "sig-ñature") is False
    assert _constant_time_text_equal(None, "") is True


def test_correlation_cap_uses_average_entry_when_mark_is_unavailable():
    from app.correlation_risk import adjusted_group_exposure
    position = SimpleNamespace(
        symbol="BTC", quantity=1.0, mark_price=0.0, average_entry_price=60_000.0
    )
    result = adjusted_group_exposure(
        [position], proposed_symbol="BTC", proposed_notional=40_000, cap_usd=50_000
    )
    assert result["existing_notional"] == 60_000.0
    assert result["projected_notional"] == 100_000.0
    assert result["allowed"] is False


def test_unvalued_correlated_position_fails_closed():
    from app.correlation_risk import adjusted_group_exposure
    position = SimpleNamespace(
        symbol="BTC", quantity=1.0, mark_price=0.0, average_entry_price=float("nan")
    )
    result = adjusted_group_exposure(
        [position], proposed_symbol="BTC", proposed_notional=1, cap_usd=50_000
    )
    assert result["valuation_complete"] is False
    assert result["allowed"] is False
    json.dumps(result, allow_nan=False)


def test_staging_and_production_model_integrity_cannot_be_disabled(tmp_path, monkeypatch):
    import app.trading_core as core
    artifact = tmp_path / "bad-model.joblib"
    artifact.write_bytes(b"not-a-model")
    meta = {
        "model_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "manifest_signature": "",
    }
    artifact.with_suffix(artifact.suffix + ".meta.json").write_text(json.dumps(meta))
    monkeypatch.setattr(core.settings, "environment", "production")
    monkeypatch.setattr(core.settings, "model_integrity_required_for_live", False)
    monkeypatch.setattr(core.settings, "model_signing_public_key", "")
    monkeypatch.setattr(core.settings, "model_signing_key", "")
    with pytest.raises(RuntimeError, match="signature is required"):
        core.load_model(str(artifact))


def test_withdrawal_destination_whitelist_is_fail_closed(monkeypatch):
    from app import withdrawal_security as security
    monkeypatch.setattr(security.settings, "environment", "staging")
    monkeypatch.setattr(security.settings, "withdrawal_whitelist_enabled", False)
    monkeypatch.setattr(security.settings, "withdrawal_destination_fingerprints", "")
    assert security.destination_allowed("TExampleAddress", "USDT", "TRON") is False
    monkeypatch.setattr(security.settings, "withdrawal_whitelist_enabled", True)
    assert security.destination_allowed("TExampleAddress", "USDT", "TRON") is False


def test_ai_provider_checker_retries_transient_503(monkeypatch):
    import io
    import urllib.error
    import scripts.check_ai_models as checker

    calls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 503, "busy", {"Retry-After": "0"},
                io.BytesIO(b'{"error":{"message":"temporarily busy"}}'),
            )
        return Response()

    monkeypatch.setattr(checker.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(checker.time, "sleep", lambda _delay: None)
    status, body = checker._request_json("https://provider.invalid/models", {}, None)
    assert status == 200
    assert body == {"ok": True}
    assert len(calls) == 2


def test_gemini_alternative_discovery_uses_only_listed_stable_flash_models():
    from scripts.check_ai_models import _select_gemini_flash_alternatives

    models = [
        {"name": "models/gemini-3.6-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.7-flash-preview", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.4-flash", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemini-3.3-pro", "supportedGenerationMethods": ["generateContent"]},
    ]
    assert _select_gemini_flash_alternatives(models, "gemini-3.6-flash") == [
        "gemini-3.5-flash", "gemini-3.5-flash-lite"
    ]


def test_production_gate_has_no_provider_quota_waiver_and_uses_verified_model():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    verify = (root / ".github" / "workflows" / "production-verification.yml").read_text()
    deploy = (root / ".github" / "workflows" / "production-deploy.yml").read_text()
    assert "temporarily waived" not in verify
    assert "GEMINI_MODEL: ${{ vars.GEMINI_MODEL || 'gemini-3.7-flash' }}" in verify
    assert "GEMINI_STRATEGY_MODEL: ${{ vars.GEMINI_STRATEGY_MODEL || 'gemini-3.7-flash' }}" in verify
    assert "GEMINI_MODEL: ${{ vars.GEMINI_MODEL || 'gemini-3.7-flash' }}" in deploy
    assert "GEMINI_STRATEGY_MODEL: ${{ vars.GEMINI_STRATEGY_MODEL || 'gemini-3.7-flash' }}" in deploy
