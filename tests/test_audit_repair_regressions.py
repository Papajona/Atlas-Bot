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


def test_native_ai_health_check_is_offline():
    from app.native_ai import native_ai_health_check
    report = native_ai_health_check()
    assert report["status"] == "PASS"
    assert report["network_calls"] == 0
    assert report["external_model_credentials_required"] is False


def test_native_ai_checker_is_not_a_remote_provider_retry_loop():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    old_entry = (root / "scripts" / "check_ai_models.py").read_text()
    checker = (root / "scripts" / "check_native_ai.py").read_text()
    assert "from scripts.check_native_ai import main" in old_entry
    for endpoint in ("generativelanguage.googleapis.com", "api.groq.com", "api.cerebras.ai"):
        assert endpoint not in old_entry + checker


def test_production_gate_requires_native_ai_offline_check_not_provider_attestation():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    verify = (root / ".github" / "workflows" / "production-verification.yml").read_text()
    deploy = (root / ".github" / "workflows" / "production-deploy.yml").read_text()
    assert "Atlas Native AI offline verification (no provider calls)" in verify
    assert "python -m scripts.check_native_ai" in verify
    assert "require_native_ai_verification" in deploy
    assert "GEMINI_API_KEY" not in verify
    assert "GROQ_API_KEY" not in verify
    assert "CEREBRAS_API_KEY" not in verify
    # The deployment can inject an optional Groq review credential, but does not
    # use Groq as a strategy generator or allow a Gemini credential back in.
    assert "GEMINI_API_KEY" not in deploy
    assert "GEMINI_STRATEGY_MODEL" not in deploy
    assert "GROQ_STRATEGY_MODEL" not in deploy
    assert "CEREBRAS_API_KEY" not in deploy
    assert "SECRET_GROQ_API_KEY_VERSION" in deploy
    assert "GROQ_API_KEY=" not in deploy


def test_paper_candidate_archive_evidence_and_trial_count_fail_closed():
    from copy import deepcopy
    from app.research_engine import paper_candidates

    valid = {
        "asset_class": "forex",
        "universe_bias": "NOT_APPLICABLE",
        "validation": {
            "walk_forward_purge_complete": True,
            "trial_registry_complete": True,
            "comparable_trial_archive": {
                "verified": True,
                "trial_archive_verified": True,
                "evidence_id": "signed-archive-2026-01",
                "evidence_sha256": "a" * 64,
            },
            "deflated_sharpe": {
                "status": "OK",
                "trial_scope": "VERIFIED_ARCHIVE_SAME_OOS_SAMPLE",
                "trial_count_archive": 3,
            },
        },
        "strategies": [],
    }
    assert paper_candidates(deepcopy(valid)) == []

    for malformed_count in ("3", "NaN", 3.0, True, None, float("nan"), -1, 1):
        research = deepcopy(valid)
        research["validation"]["deflated_sharpe"]["trial_count_archive"] = malformed_count
        assert paper_candidates(research) == []

    invalid_archives = [
        {"verified": False, "trial_archive_verified": True, "evidence_id": "signed", "evidence_sha256": "a" * 64},
        {"verified": True, "trial_archive_verified": False, "evidence_id": "signed", "evidence_sha256": "a" * 64},
        {"verified": True, "trial_archive_verified": True, "evidence_id": "", "evidence_sha256": "a" * 64},
        {"verified": True, "trial_archive_verified": True, "evidence_id": "signed", "evidence_sha256": "g" * 64},
    ]
    for archive in invalid_archives:
        research = deepcopy(valid)
        research["validation"]["comparable_trial_archive"] = archive
        assert paper_candidates(research) == []


def test_trial_archive_row_uses_report_protocol_version(monkeypatch):
    from app.research_trial_archive import _report_context, _row_payload

    monkeypatch.setenv("ATLAS_CODE_SHA256", "d" * 64)
    report = {
        "asset_class": "forex",
        "data_provenance": {
            "sha256": "b" * 64,
            "exchange": "oanda",
            "market_type": "practice",
            "symbol": "EUR_USD",
            "timeframe": "1h",
        },
        "validation": {
            "protocol_version": "atlas-purged-walk-forward-v2",
            "oos_sample_sha256": "c" * 64,
        },
        "config": {"window": 20},
    }
    request = SimpleNamespace(
        asset="forex", exchange="oanda", market_type="practice",
        symbol="EUR_USD", timeframe="1h",
    )
    context = _report_context(report, request)
    payload = _row_payload(
        report, context, "trend@config", {"sharpe": 0.5, "oos_total_return": 0.1,
        "max_drawdown": -0.1, "trades": 12}, "trend", "evaluation-id",
    )
    assert context["protocol_version"] == "atlas-purged-walk-forward-v2"
    assert payload["protocol_version"] == context["protocol_version"]


def test_ai_trial_labels_are_preserved_by_prospective_trial_writer():
    from app.research_trial_archive import _trial_specs

    report = {
        "validation": {
            "trial_labels": ["trend@config", "ai@config"],
            "per_strategy_oos": {"trend": {"sharpe": 0.5}},
        },
        "ai_walk_forward": {"strategy": "ai_walk_forward", "sharpe": 0.7, "status": "OK"},
    }
    specs = _trial_specs(report)
    ai_specs = [spec for spec in specs if spec[0] == "ai@config"]
    assert len(ai_specs) == 1
    assert ai_specs[0][1]["strategy"] == "ai_walk_forward"
    assert ai_specs[0][2] == "ai"
