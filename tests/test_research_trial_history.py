from __future__ import annotations

import numpy as np

import app.trading_core as trading_core


def fake_provenance():
    return {
        "raw_response_sha256": "a" * 64,
        "normalized_csv_sha256": "b" * 64,
        "instrument": "XAU_USD",
        "granularity": "D",
        "bars_per_year": 252,
        "evaluation_assumptions": {
            "bars_per_year": 252,
            "taker_bps": 0.5,
            "slippage_bps": 0.5,
            "carry_bps_per_bar": 0.0,
            "max_hold_bars": 24,
        },
        "request_parameters": {"price": "M", "from": "2020-01-01T00:00:00Z", "to": "2025-01-01T00:00:00Z"},
    }


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
        load_complete_trial_history(ledger, 1, archive, fake_provenance())

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
        load_complete_trial_history(ledger, 2, archive, fake_provenance())


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
        load_complete_trial_history(ledger, 1, archive, fake_provenance())


def test_oos_gate_adapter_preserves_fold_metrics_and_cost_stress():
    from scripts.evaluate_original_oanda_oos import promotion_gate_metrics

    folds = [{"total_return": 0.01}, {"total_return": 0.02}]
    original = {"fold_metrics": folds, "cost_analysis": {"stress_ok": True}}
    normalized = promotion_gate_metrics(original)
    assert normalized["folds"] == folds
    assert normalized["cost_stress_ok"] is True
    assert original.get("folds") is None


def test_oos_gate_adapter_fails_closed_when_cost_stress_is_missing():
    from scripts.evaluate_original_oanda_oos import promotion_gate_metrics

    assert promotion_gate_metrics({"fold_metrics": []})["cost_stress_ok"] is False


def test_oos_gate_adapter_preserves_actual_walk_forward_backtest_fold_key():
    from scripts.evaluate_original_oanda_oos import promotion_gate_metrics

    emitted_folds = [
        {"total_return": 0.01},
        {"total_return": 0.02},
        {"total_return": 0.03},
    ]
    backtest_metrics = {
        "folds": emitted_folds,
        "cost_analysis": {"stress_ok": True},
    }
    normalized = promotion_gate_metrics(backtest_metrics)
    assert normalized["folds"] == emitted_folds
    assert normalized["cost_stress_ok"] is True


def test_trial_ledger_must_match_manifest_inside_hashed_experiment_archive(tmp_path):
    import hashlib
    import json
    from zipfile import ZipFile
    from scripts.evaluate_original_oanda_oos import load_complete_trial_history

    provenance = fake_provenance()
    archive = tmp_path / "experiment.zip"
    archived_manifest = {
        "schema_version": 1,
        "complete": True,
        "all_variants_included": True,
        "experiment_id": "experiment-valid",
        "dataset_raw_response_sha256": provenance["raw_response_sha256"],
        "dataset_csv_sha256": provenance["normalized_csv_sha256"],
        "instrument": provenance["instrument"],
        "granularity": provenance["granularity"],
        "request_parameters": provenance["request_parameters"],
        "evaluation_assumptions": provenance["evaluation_assumptions"],
        "variants": [
            {"variant_id": "feature-set-a", "annualized_sharpe": 0.8},
            {"variant_id": "feature-set-b", "annualized_sharpe": -0.1},
        ],
    }
    with ZipFile(archive, "w") as stream:
        stream.writestr("atlas_trial_manifest.json", json.dumps(archived_manifest))
        stream.writestr("experiment-notes.txt", "full experiment artifacts are archived")
    ledger = tmp_path / "trials.json"
    ledger.write_text(json.dumps({
        "schema_version": 1,
        "experiment_id": "experiment-valid",
        "experiment_archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "complete": True,
        "all_variants_included": True,
        "variants": archived_manifest["variants"],
    }), encoding="utf-8")

    sharpes, evidence = load_complete_trial_history(ledger, 2, archive, provenance)
    assert sharpes == [0.8, -0.1]
    assert evidence["archive_manifest"]["trial_list_matches_archive"] is True
    assert evidence["archive_manifest"]["data_identity_matches"] is True


def test_trial_ledger_rejects_archive_with_wrong_dataset_identity(tmp_path):
    import hashlib
    import json
    from zipfile import ZipFile
    import pytest
    from scripts.evaluate_original_oanda_oos import load_complete_trial_history

    provenance = fake_provenance()
    archive = tmp_path / "experiment.zip"
    archived_manifest = {
        "schema_version": 1, "complete": True, "all_variants_included": True,
        "experiment_id": "experiment-wrong-data",
        "dataset_raw_response_sha256": "f" * 64,
        "dataset_csv_sha256": provenance["normalized_csv_sha256"],
        "instrument": provenance["instrument"], "granularity": provenance["granularity"],
        "request_parameters": provenance["request_parameters"],
        "evaluation_assumptions": provenance["evaluation_assumptions"],
        "variants": [{"variant_id": "base", "annualized_sharpe": 0.7}],
    }
    with ZipFile(archive, "w") as stream:
        stream.writestr("atlas_trial_manifest.json", json.dumps(archived_manifest))
    ledger = tmp_path / "trials.json"
    ledger.write_text(json.dumps({
        "schema_version": 1, "experiment_id": archived_manifest["experiment_id"],
        "experiment_archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "complete": True, "all_variants_included": True,
        "variants": archived_manifest["variants"],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="dataset_raw_response_sha256"):
        load_complete_trial_history(ledger, 1, archive, provenance)


def test_trial_ledger_rejects_variants_missing_from_archive_manifest(tmp_path):
    import hashlib
    import json
    from zipfile import ZipFile
    import pytest
    from scripts.evaluate_original_oanda_oos import load_complete_trial_history

    provenance = fake_provenance()
    archive = tmp_path / "experiment.zip"
    archived_manifest = {
        "schema_version": 1, "complete": True, "all_variants_included": True,
        "experiment_id": "experiment-omission",
        "dataset_raw_response_sha256": provenance["raw_response_sha256"],
        "dataset_csv_sha256": provenance["normalized_csv_sha256"],
        "instrument": provenance["instrument"], "granularity": provenance["granularity"],
        "request_parameters": provenance["request_parameters"],
        "evaluation_assumptions": provenance["evaluation_assumptions"],
        "variants": [{"variant_id": "base", "annualized_sharpe": 0.7}],
    }
    with ZipFile(archive, "w") as stream:
        stream.writestr("atlas_trial_manifest.json", json.dumps(archived_manifest))
    ledger = tmp_path / "trials.json"
    ledger.write_text(json.dumps({
        "schema_version": 1, "experiment_id": archived_manifest["experiment_id"],
        "experiment_archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "complete": True, "all_variants_included": True,
        "variants": [
            {"variant_id": "base", "annualized_sharpe": 0.7},
            {"variant_id": "forgotten-variant", "annualized_sharpe": 1.6},
        ],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="do not exactly match"):
        load_complete_trial_history(ledger, 2, archive, provenance)


def test_walk_forward_rejects_fractional_and_coerced_annualization_or_horizon():
    import pandas as pd
    import pytest

    from app.trading_core import ai_walk_forward_backtest

    for invalid in (252.5, 252.0, "252", True):
        with pytest.raises(ValueError, match="Invalid asset or walk-forward evaluation assumptions"):
            ai_walk_forward_backtest(pd.DataFrame(), bars_per_year=invalid)
    for invalid in (10.5, 10.0, "10", True):
        with pytest.raises(ValueError, match="Invalid asset or walk-forward evaluation assumptions"):
            ai_walk_forward_backtest(pd.DataFrame(), max_hold_bars=invalid)
