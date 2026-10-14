from __future__ import annotations

import numpy as np

import app.trading_core as trading_core


def test_dsr_is_not_reportable_without_full_trial_history():
    report = trading_core._research_trial_dsr_report(
        np.linspace(-0.01, 0.01, 80),
        None,
        252,
    )
    assert report["status"] == "MISSING_TRIAL_HISTORY"
    assert "deflated_sharpe_ratio" not in report


def test_dsr_uses_all_variant_sharpes_not_fold_sharpes(monkeypatch):
    calls = {}

    def fake_report(net_returns, trial_sharpes_annual, n_trials, bars_per_year):
        calls["trial_sharpes"] = trial_sharpes_annual
        calls["n_trials"] = n_trials
        calls["bars_per_year"] = bars_per_year
        return {"status": "OK", "deflated_sharpe_ratio": 0.97}

    monkeypatch.setattr(trading_core, "deflated_sharpe_report", fake_report)
    variants = [0.1, 0.2, -0.05, 0.7, 0.4, 0.0]
    report = trading_core._research_trial_dsr_report(
        np.linspace(-0.01, 0.01, 80),
        variants,
        252,
    )
    assert report["status"] == "OK"
    assert report["deflated_sharpe_ratio"] == 0.97
    assert calls == {
        "trial_sharpes": variants,
        "n_trials": len(variants),
        "bars_per_year": 252,
    }


def test_dsr_rejects_non_finite_trial_history():
    report = trading_core._research_trial_dsr_report(
        np.linspace(-0.01, 0.01, 80),
        [0.2, float("nan")],
        252,
    )
    assert report["status"] == "INVALID_TRIAL_HISTORY"


def test_reference_manifest_must_be_separate_from_candidate(tmp_path):
    import pytest
    from scripts.evaluate_original_oanda_oos import verify_reference_identity

    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="separately archived"):
        verify_reference_identity(tmp_path, manifest, 1799)


def test_trial_ledger_rejects_incomplete_or_duplicate_variants(tmp_path):
    import hashlib
    import json
    import pytest
    from scripts.evaluate_original_oanda_oos import load_complete_trial_history

    archive = tmp_path / "experiment.zip"
    archive.write_bytes(b"immutable experiment archive bytes")
    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    ledger = tmp_path / "trials.json"
    payload = {
        "schema_version": 1,
        "experiment_id": "experiment-001",
        "experiment_archive_sha256": archive_hash,
        "complete": False,
        "all_variants_included": True,
        "variants": [{"variant_id": "base", "annualized_sharpe": 0.8}],
    }
    ledger.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="complete=true"):
        load_complete_trial_history(ledger, 1, archive)

    payload.update({
        "complete": True,
        "all_variants_included": True,
        "variants": [
            {"variant_id": "base", "annualized_sharpe": 0.8},
            {"variant_id": "base", "annualized_sharpe": 0.7},
        ],
    })
    ledger.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_complete_trial_history(ledger, 2, archive)


def test_trial_ledger_requires_exact_experiment_archive_hash(tmp_path):
    import json
    import pytest
    from scripts.evaluate_original_oanda_oos import load_complete_trial_history

    archive = tmp_path / "experiment.zip"
    archive.write_bytes(b"actual archived data")
    ledger = tmp_path / "trials.json"
    ledger.write_text(json.dumps({
        "schema_version": 1,
        "experiment_id": "experiment-002",
        "experiment_archive_sha256": "0" * 64,
        "complete": True,
        "all_variants_included": True,
        "variants": [{"variant_id": "base", "annualized_sharpe": 0.7}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        load_complete_trial_history(ledger, 1, archive)
