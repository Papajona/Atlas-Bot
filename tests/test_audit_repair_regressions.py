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


def test_settings_allow_only_single_gemini_3_8_flash_model():
    from pydantic import ValidationError
    from app.config import Settings

    valid = Settings(environment="test", gemini_model="gemini-3.8-flash")
    assert valid.gemini_model == "gemini-3.8-flash"

    for unapproved in (
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.7-flash",
        "gemini-flash-latest",
    ):
        with pytest.raises(ValidationError, match="Gemini model must be exactly gemini-3.8-flash"):
            Settings(environment="test", gemini_model=unapproved)


def test_gemini_failure_does_not_probe_alternative_models(monkeypatch):
    from app.config import settings
    from scripts import check_ai_models as checker

    monkeypatch.setattr(settings, "gemini_api_key", "configured")
    monkeypatch.setattr(settings, "groq_api_key", "configured")
    monkeypatch.setattr(settings, "gemini_model", "gemini-3.8-flash")
    monkeypatch.setattr(settings, "groq_model", "openai/gpt-oss-20b")
    monkeypatch.setattr(settings, "groq_strategy_model", "openai/gpt-oss-20b")
    monkeypatch.setattr(settings, "cerebras_api_key", "")
    gemini_calls = []
    monkeypatch.setattr(checker, "_check_gemini", lambda model: gemini_calls.append(model) or False)
    monkeypatch.setattr(checker, "_check_groq", lambda _model: True)
    monkeypatch.setattr(
        checker,
        "_request_json",
        lambda *args, **kwargs: pytest.fail("A failed Gemini check must not probe the model catalogue or alternatives"),
    )

    assert checker.main() == 1
    assert gemini_calls == ["gemini-3.8-flash"]



def test_production_gate_has_no_provider_quota_waiver_and_uses_verified_model():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    verify = (root / ".github" / "workflows" / "production-verification.yml").read_text()
    deploy = (root / ".github" / "workflows" / "production-deploy.yml").read_text()
    assert "temporarily waived" not in verify
    provider_step = verify.split("id: ai_provider_verification", 1)[1].split("\n      - name:", 1)[0]
    assert "continue-on-error: true" not in provider_step
    assert "GEMINI_MODEL: gemini-3.8-flash" in verify
    assert "GEMINI_MODEL: gemini-3.8-flash" in deploy
    assert "GEMINI_STRATEGY_MODEL" not in verify + deploy
    assert "vars.GEMINI_MODEL" not in verify + deploy
    assert "require_manual_provider_verification" in deploy
    assert "require_success \"production-verification.yml\"" not in deploy
    assert "--event workflow_dispatch" in deploy
    assert "--commit \"$GITHUB_SHA\"" in deploy
    assert "provider_conclusion" in deploy
    assert '.jobs[].steps[] | select(.name == "AI model/provider verification when credentials are supplied") | .conclusion' in deploy
    api_deploy = (root / "deploy" / "cloud-run-deploy.sh").read_text()
    worker_deploy = (root / "deploy" / "cloud-run-worker-deploy.sh").read_text()
    for deployment in (api_deploy, worker_deploy):
        assert "GEMINI_MODEL=gemini-3.8-flash" in deployment
        assert "GEMINI_STRATEGY_MODEL" not in deployment
        assert "${GEMINI_MODEL:-" not in deployment
    env_example = (root / "deploy" / "cloud-run.env.example").read_text()
    runbook = (root / "deploy" / "DEPLOY_WORKFLOW.md").read_text()
    assert "GEMINI_MODEL=gemini-3.8-flash" in env_example
    assert "GEMINI_STRATEGY_MODEL" not in env_example
    assert "| `GEMINI_MODEL` | `gemini-3.8-flash` |" in runbook



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
