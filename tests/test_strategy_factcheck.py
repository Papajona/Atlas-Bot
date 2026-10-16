from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

def frame(n=500, freq="h"):
    idx=pd.date_range("2025-01-01", periods=n, freq=freq, tz="UTC")
    x=np.linspace(100,120,n)
    return pd.DataFrame({"open":x,"high":x+1,"low":x-1,"close":x}, index=idx)

def frame_volume(n=500):
    df=frame(n); df["volume"]=100.0; return df

def test_strategy_lab_no_keyword_parser():
    s=(ROOT/"app"/"main.py").read_text()
    block=s[s.index('@app.post("/api/customer/strategy-lab/candidates")'):s.index('@app.post("/api/customer/strategy-lab/candidates/{candidate_id}/validate")')]
    assert "generate_strategy_draft" in block
    assert "text=req.prompt.lower()" not in block
    assert 'indicators":[k.upper()' not in block

def test_ai_schema_has_supported_strategy_family():
    from app.strategy_ai import AIStrategySpec
    raw={"version":"atlas-strategy-v1","title":"Test","summary":"A test strategy with explicit rules and risk controls.","asset":"crypto","symbols":["BTC/USDT:USDT"],"timeframes":["1h"],"direction":"LONG","strategy_family":"TREND","entry_conditions":[{"type":"trend","rule":"Price above the 200 EMA","timeframe":"1h"}],"exit_conditions":[{"type":"stop","rule":"Stop at 2 ATR","timeframe":"1h"}],"risk_per_trade":0.005,"stop_loss_atr":2,"take_profit_rr":2,"sessions":[],"assumptions":[],"validation_warnings":[],"requires_backtest":True,"requires_walk_forward":True,"execution_mode":"PAPER_SHADOW_ONLY","implementation_status":"ATLAS_SUPPORTED_FAMILY"}
    out=AIStrategySpec.model_validate(raw)
    assert out.strategy_family=="TREND"

def test_rsi_monotonic_up_is_100():
    from app.strategy_engine import _rsi
    close=pd.Series(np.arange(1,40,dtype=float))
    r=_rsi(close,14).dropna()
    assert float(r.iloc[-1])==100.0

def test_vwap_missing_volume_is_not_fabricated():
    from app.strategy_engine import strategy_signals
    s=strategy_signals(frame(600))
    assert not bool(s["vwap_available"].any())
    assert np.allclose(s["vwap_signal"].fillna(0),0)

def test_opening_range_not_available_before_session_hour_closes():
    from app.strategy_engine import strategy_signals
    idx=pd.date_range("2025-01-06 06:00", "2025-01-06 10:30", freq="15min", tz="UTC")
    x=np.arange(len(idx),dtype=float)+100
    df=pd.DataFrame({"open":x,"high":x+1,"low":x-1,"close":x,"volume":100},index=idx)
    s=strategy_signals(df)
    london=s["london_range_high"]
    # 08:00-09:00 local London in January is 08:00-09:00 UTC; range is only known after 09:00.
    assert london.loc["2025-01-06 08:45+00:00"] != london.loc["2025-01-06 08:45+00:00"]
    assert np.isfinite(float(london.loc["2025-01-06 09:00+00:00"]))

def test_higher_timeframe_mapping_applies_source_lag():
    from app.multi_timeframe import _map_back
    src=pd.Series([10.0,20.0], index=pd.to_datetime(["2025-01-01 01:00","2025-01-01 02:00"], utc=True))
    idx=pd.date_range("2025-01-01 00:30", periods=4, freq="30min", tz="UTC")
    out=_map_back(src,idx)
    # The 02:00 source value is not available at 02:00 itself under the conservative rule.
    assert out.loc["2025-01-01 02:00+00:00"]==10.0

def test_paper_candidates_use_complete_oos_metrics_and_ignore_full_sample_scores(monkeypatch):
    from app import research_engine
    from app.research_engine import paper_candidates
    row={"strategy":"trend","sharpe":1.0,"max_drawdown":-0.1,"trades":150,"total_return":0.2}
    passing_folds=[{"total_return":0.05},{"total_return":0.04},{"total_return":-0.01},{"total_return":0.03},{"total_return":0.02}]
    base={"asset_class":"forex","universe_bias":"NOT_APPLICABLE","strategies":[row],"validation":{"trial_registry_complete":True,"deflated_sharpe":{"status":"OK","deflated_sharpe_ratio":0.99},"per_strategy_oos":{"trend":{"status":"OK","oos_total_return":0.1,"sharpe":0.9,"max_drawdown":-0.1,"trades":150,"deflated_sharpe":0.99,"cost_stress_ok":True,"financing_cost_model_complete":True,"intrabar_stop_target_model_complete":True,"execution_cost_model_complete":True,"execution_cost_calibration_status":"VERIFIED","execution_cost_calibration_evidence_id":"fixture:venue-cost-calibration","trial_registry_complete":True,"folds":passing_folds}}}}
    assert paper_candidates(base) == []  # no trusted signed-archive verifier is integrated
    bad={"strategies":[row],"validation":{"per_strategy_oos":{"trend":{"status":"OK","oos_total_return":-0.1,"folds":[{"total_return":0.05},{"total_return":-0.2}]}}}}
    assert paper_candidates(bad)==[]

    monkeypatch.setattr(research_engine.settings, "research_folds", 5)
    # Great held-out metrics and poor in-sample metrics do not override the
    # missing signed comparable-trial archive or survivorship-biased universe.
    row = {"strategy": "trend", "sharpe": -3.0, "max_drawdown": -0.90,
           "trades": 0, "total_return": -0.80}
    passing_folds = [
        {"total_return": 0.05}, {"total_return": 0.04}, {"total_return": -0.01},
        {"total_return": 0.03}, {"total_return": 0.02}, {"total_return": 0.01},
    ]
    oos = {
        "status": "OK", "oos_total_return": 0.10, "sharpe": 0.9,
        "max_drawdown": -0.12, "trades": 150, "folds": passing_folds,
        "deflated_sharpe": 0.99, "cost_stress_ok": True,
        "financing_cost_model_complete": True,
        "intrabar_stop_target_model_complete": True,
        "execution_cost_model_complete": True,
        "execution_cost_calibration_status": "VERIFIED",
        "execution_cost_calibration_evidence_id": "fixture:venue-cost-calibration",
        "trial_registry_complete": True,
    }
    research = {
        "asset_class": "crypto",
        "universe_bias": "CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY",
        "strategies": [row],
        "validation": {
            "trial_registry_complete": True,
            "deflated_sharpe": {"status": "OK", "deflated_sharpe_ratio": 0.99},
            "per_strategy_oos": {"trend": oos},
        },
    }

    # This path has no trusted signed archive verifier configured. Report fields
    # and synthetic fixture values must not produce a candidate.
    assert paper_candidates(research) == []


def test_paper_candidates_reverify_original_signed_evidence(monkeypatch):
    import base64
    import hashlib
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from app import research_engine
    from app.research_engine import paper_candidates
    from app.research_evidence import (
        canonical_json_bytes, canonical_sha256, validate_comparable_trial_archive,
        validate_crypto_universe_manifest, validate_execution_cost_calibration,
    )

    monkeypatch.setattr(research_engine.settings, "research_folds", 5)
    monkeypatch.setattr(research_engine.settings, "research_cost_max_spread_mae_bps", 1.0)
    monkeypatch.setattr(research_engine.settings, "research_cost_max_slippage_mae_bps", 1.0)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    trusted = {"research-test": base64.b64encode(public).decode("ascii")}

    def sign(document):
        signature = key.sign(canonical_json_bytes(document))
        return {
            **document,
            "attestation": {
                "algorithm": "Ed25519",
                "key_id": "research-test",
                "signature_base64": base64.b64encode(signature).decode("ascii"),
            },
        }

    def source(kind):
        return {
            "source_type": kind,
            "uri": f"https://evidence-source.atlas-data.net/{kind}/source.csv",
            "sha256": hashlib.sha256(kind.encode()).hexdigest(),
        }

    dataset_sha = hashlib.sha256(b"exact-dataset").hexdigest()
    sample_sha = hashlib.sha256(b"exact-oos-sample").hexdigest()
    code_sha = hashlib.sha256(b"exact-code").hexdigest()
    start, end = "2024-01-01T00:00:00Z", "2025-01-01T00:00:00Z"
    instruments = [
        {"symbol": "BTC/USDT", "listed_at": "2017-01-01T00:00:00Z",
         "delisted_at": None, "failed_at": None},
        {"symbol": "OLD/USDT", "listed_at": "2020-01-01T00:00:00Z",
         "delisted_at": "2024-06-01T00:00:00Z", "failed_at": None},
    ]
    universe_doc = sign({
        "evidence_type": "crypto_point_in_time_universe", "evidence_id": "universe-test",
        "venue": "binance", "market_type": "spot", "coverage_start": start, "coverage_end": end,
        "membership_policy": "POINT_IN_TIME", "coverage_complete": True,
        "includes_delisted": True, "includes_failed_assets": True,
        "sources": [source(kind) for kind in ("listing_history", "delisting_history", "failed_assets")],
        "instruments": instruments,
        "instruments_sha256": canonical_sha256(sorted(instruments, key=lambda row: row["symbol"].casefold())),
    })
    universe_checked = validate_crypto_universe_manifest(
        universe_doc, trusted, venue="binance", market_type="spot",
        coverage_start=start, coverage_end=end, symbol="BTC/USDT",
    )
    assert universe_checked["universe_verified"] is True

    fingerprint = hashlib.sha256(b"atlas-cost-model").hexdigest()
    cost_doc = sign({
        "evidence_type": "venue_execution_cost_calibration", "evidence_id": "cost-test",
        "venue": "binance", "market_type": "spot", "symbol": "BTC/USDT",
        "model_fingerprint": fingerprint, "synthetic_only": False, "testnet_only": False,
        "sources": [source(kind) for kind in ("observed_fills", "order_book", "fee_schedule")],
        "observed_order_count": 40, "observed_fill_count": 35,
        "validation_start": start, "validation_end": end,
        "heldout_validation": {
            "status": "PASS", "sample_count": 35,
            "mean_absolute_spread_error_bps": 0.2,
            "mean_absolute_slippage_error_bps": 0.3,
        },
    })
    cost_checked = validate_execution_cost_calibration(
        cost_doc, trusted, venue="binance", market_type="spot", symbol="BTC/USDT",
        model_fingerprint=fingerprint, max_spread_error_bps=1.0, max_slippage_error_bps=1.0,
    )
    assert cost_checked["calibration_verified"] is True

    config_sha = hashlib.sha256(b"strategy-config").hexdigest()
    trial_labels = [f"{name}@{config_sha}" for name in (
        "trend", "momentum", "breakout", "mean_reversion", "ensemble",
    )] + [f"variant:{m}@{config_sha}" for m in ("0.8", "0.9", "1.0", "1.1", "1.2")] + [f"ai@{config_sha}"]
    archive_rows = [{
        "trial_label": label,
        "config_sha256": hashlib.sha256((label + "-config").encode()).hexdigest(),
        "code_sha256": code_sha,
        "dataset_sha256": dataset_sha,
        "oos_sample_sha256": sample_sha,
        "protocol_version": "atlas-purged-walk-forward-v1",
        "annualized_sharpe": 0.4,
        "results_sha256": hashlib.sha256((label + "-results").encode()).hexdigest(),
        "asset_class": "crypto", "venue": "binance", "market_type": "spot",
        "symbol": "BTC/USDT", "timeframe": "1h",
    } for label in trial_labels]
    archive_doc = sign({
        "evidence_type": "comparable_trial_performance_archive", "evidence_id": "archive-test",
        "archive_complete": True, "includes_all_trials": True,
        "trial_count": len(archive_rows), "trials": archive_rows,
    })
    context = {
        "asset_class": "crypto", "venue": "binance", "market_type": "spot",
        "symbol": "BTC/USDT", "timeframe": "1h", "dataset_sha256": dataset_sha,
        "oos_sample_sha256": sample_sha, "protocol_version": "atlas-purged-walk-forward-v1",
    }
    archive_checked = validate_comparable_trial_archive(
        archive_doc, trusted, candidate_labels=set(trial_labels), expected_context=context,
    )
    assert archive_checked["trial_archive_verified"] is True

    folds = [
        {"total_return": 0.05}, {"total_return": 0.04}, {"total_return": 0.03},
        {"total_return": -0.01}, {"total_return": 0.02}, {"total_return": 0.01},
    ]
    oos = {
        "status": "OK", "oos_total_return": 0.10, "sharpe": 0.9,
        "max_drawdown": -0.12, "trades": 150, "folds": folds,
        "deflated_sharpe": 0.99, "cost_stress_ok": True,
        "financing_cost_model_complete": True, "intrabar_stop_target_model_complete": True,
        "execution_cost_model_complete": True, "execution_cost_calibration_status": "VERIFIED",
        "execution_cost_calibration_evidence_id": cost_checked["evidence_id"],
        "trial_registry_complete": True, "slippage_model_complete": True,
        "universe_bias": "POINT_IN_TIME_VERIFIED",
    }
    research = {
        "asset_class": "crypto", "universe_bias": "POINT_IN_TIME_VERIFIED",
        "exchange": "binance", "symbol": "BTC/USDT", "timeframe": "1h",
        "data_provenance": {
            "sha256": dataset_sha, "exchange": "binance", "market_type": "spot",
            "symbol": "BTC/USDT", "timeframe": "1h",
        },
        "strategies": [{"strategy": "trend", "sharpe": -3.0, "max_drawdown": -0.9,
                        "trades": 0, "total_return": -0.8}],
        "validation": {
            "trial_labels": trial_labels, "oos_sample_sha256": sample_sha,
            "trial_registry_complete": True,
            "deflated_sharpe": {"status": "OK", "deflated_sharpe_ratio": 0.99},
            "crypto_universe_validation": universe_checked,
            "execution_cost_model_fingerprint": fingerprint,
            "execution_cost_calibration": cost_checked,
            "comparable_trial_archive": archive_checked,
            "per_strategy_oos": {"trend": oos},
        },
    }
    # All report flags are insufficient without the original documents and trusted keys.
    assert paper_candidates(research) == []
    candidates = paper_candidates(
        research,
        trial_archive_document=archive_doc,
        execution_cost_calibration_evidence=cost_doc,
        point_in_time_universe_manifest=universe_doc,
        trusted_public_keys=trusted,
        universe_coverage_start=start,
        universe_coverage_end=end,
        min_sharpe=0.5, max_drawdown=-0.25, min_trades=20,
        min_oos_total_return=0.0, min_oos_positive_fold_ratio=0.5,
    )
    assert [candidate["strategy"] for candidate in candidates] == ["trend"]
    # A report-level calibrated status cannot hide stale per-strategy provenance.
    original_cost_id = oos["execution_cost_calibration_evidence_id"]
    oos["execution_cost_calibration_evidence_id"] = "different-evidence"
    assert paper_candidates(
        research, trial_archive_document=archive_doc, execution_cost_calibration_evidence=cost_doc,
        point_in_time_universe_manifest=universe_doc, trusted_public_keys=trusted,
        universe_coverage_start=start, universe_coverage_end=end,
        min_sharpe=0.5, max_drawdown=-0.25, min_trades=20,
        min_oos_total_return=0.0, min_oos_positive_fold_ratio=0.5,
    ) == []
    oos["execution_cost_calibration_evidence_id"] = original_cost_id
    oos["universe_bias"] = "CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY"
    oos["survivorship_bias_blocked"] = True
    assert paper_candidates(
        research, trial_archive_document=archive_doc, execution_cost_calibration_evidence=cost_doc,
        point_in_time_universe_manifest=universe_doc, trusted_public_keys=trusted,
        universe_coverage_start=start, universe_coverage_end=end,
        min_sharpe=0.5, max_drawdown=-0.25, min_trades=20,
        min_oos_total_return=0.0, min_oos_positive_fold_ratio=0.5,
    ) == []
    oos["universe_bias"] = "POINT_IN_TIME_VERIFIED"
    oos["survivorship_bias_blocked"] = False
    # Signature or context changes after report generation must stop candidacy.
    forged = dict(archive_doc, archive_complete=False)
    assert paper_candidates(
        research, trial_archive_document=forged, execution_cost_calibration_evidence=cost_doc,
        point_in_time_universe_manifest=universe_doc, trusted_public_keys=trusted,
        universe_coverage_start=start, universe_coverage_end=end,
        min_sharpe=0.5, max_drawdown=-0.25, min_trades=20,
        min_oos_total_return=0.0, min_oos_positive_fold_ratio=0.5,
    ) == []

def test_paper_candidates_reject_strong_in_sample_result_when_oos_metrics_fail(monkeypatch):
    from app import research_engine
    from app.research_engine import paper_candidates

    monkeypatch.setattr(research_engine.settings, "research_folds", 5)
    row = {"strategy": "trend", "sharpe": 2.0, "max_drawdown": -0.05,
           "trades": 500, "total_return": 1.2}
    oos = {
        "status": "OK", "oos_total_return": 0.10, "sharpe": -0.4,
        "max_drawdown": -0.60, "trades": 10,
        "folds": [{"total_return": 0.05}, {"total_return": 0.04},
                  {"total_return": -0.01}, {"total_return": 0.03},
                  {"total_return": 0.02}],
    }
    research = {"strategies": [row], "validation": {"per_strategy_oos": {"trend": oos}}}
    assert paper_candidates(research) == []


def test_paper_candidates_fail_closed_on_missing_or_malformed_oos_metrics(monkeypatch):
    from app import research_engine
    from app.research_engine import paper_candidates

    monkeypatch.setattr(research_engine.settings, "research_folds", 5)
    row = {"strategy": "trend", "sharpe": 5.0, "max_drawdown": -0.01,
           "trades": 500, "total_return": 5.0}
    cases = [
        {"status": "OK", "oos_total_return": 1.0, "sharpe": 1.0,
         "max_drawdown": -0.1, "trades": 200, "folds": [{"total_return": 0.1}] * 4},
        {"status": "OK", "oos_total_return": 1.0, "max_drawdown": -0.1,
         "trades": 200, "folds": [{"total_return": 0.1}] * 5},
        {"status": "OK", "oos_total_return": 1.0, "sharpe": float("nan"),
         "max_drawdown": -0.1, "trades": 200, "folds": [{"total_return": 0.1}] * 5},
        {"status": "OK", "oos_total_return": 1.0, "sharpe": 1.0,
         "max_drawdown": -0.1, "trades": 200,
         "folds": [{"total_return": 0.1}, {"total_return": 0.1},
                   {"total_return": float("inf")}, {"total_return": 0.1},
                   {"total_return": 0.1}]},
    ]
    for oos in cases:
        research = {"strategies": [row], "validation": {"per_strategy_oos": {"trend": oos}}}
        assert paper_candidates(research) == []
