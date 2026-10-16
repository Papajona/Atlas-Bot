import base64
import hashlib
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.research_evidence import (
    canonical_json_bytes, canonical_sha256, validate_comparable_trial_archive,
    validate_crypto_universe_manifest, validate_execution_cost_calibration,
)


def _sign(document):
    key = Ed25519PrivateKey.generate()
    signature = key.sign(canonical_json_bytes(document))
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    signed = {**document, "attestation": {
        "algorithm": "Ed25519", "key_id": "test-key",
        "signature_base64": base64.b64encode(signature).decode("ascii")}}
    return signed, {"test-key": base64.b64encode(public).decode("ascii")}


def _source(kind):
    return {"source_type": kind, "uri": f"https://evidence-source.atlas-data.net/{kind}/source",
            "sha256": hashlib.sha256(kind.encode()).hexdigest()}


def _window():
    return ("2024-01-01T00:00:00Z", "2025-01-01T00:00:00Z")


def test_signed_point_in_time_universe_verifies_and_rejects_forgery():
    start, end = _window()
    instruments = [
        {"symbol": "BTC/USDT", "listed_at": "2017-01-01T00:00:00Z", "delisted_at": None, "failed_at": None},
        {"symbol": "OLD/USDT", "listed_at": "2020-01-01T00:00:00Z",
         "delisted_at": "2024-06-01T00:00:00Z", "failed_at": None},
    ]
    doc = {
        "evidence_type": "crypto_point_in_time_universe", "evidence_id": "test-universe-v1",
        "venue": "binance", "market_type": "spot", "coverage_start": start, "coverage_end": end,
        "membership_policy": "POINT_IN_TIME", "coverage_complete": True,
        "includes_delisted": True, "includes_failed_assets": True,
        "sources": [_source(k) for k in ("listing_history", "delisting_history", "failed_assets")],
        "instruments": instruments,
        "instruments_sha256": canonical_sha256(sorted(instruments, key=lambda row: row["symbol"].casefold())),
    }
    signed, keys = _sign(doc)
    args = dict(venue="binance", market_type="spot", coverage_start=start, coverage_end=end,
                symbol="BTC/USDT")
    assert validate_crypto_universe_manifest(signed, keys, **args)["universe_verified"] is True
    forged = dict(signed, includes_delisted=False)
    assert validate_crypto_universe_manifest(forged, keys, **args)["universe_verified"] is False
    doc["includes_delisted"] = False
    doc.pop("instruments_sha256")
    resigned, keys = _sign({**doc, "instruments_sha256": canonical_sha256(sorted(instruments, key=lambda row: row["symbol"].casefold()))})
    assert validate_crypto_universe_manifest(resigned, keys, **args)["universe_verified"] is False


def test_signed_cost_calibration_requires_real_venue_data_and_matching_model():
    start, end = _window()
    doc = {
        "evidence_type": "venue_execution_cost_calibration", "evidence_id": "cost-v1",
        "venue": "binance", "market_type": "spot", "symbol": "BTC/USDT",
        "model_fingerprint": "a" * 64, "synthetic_only": True, "testnet_only": False,
        "sources": [_source(k) for k in ("observed_fills", "order_book", "fee_schedule")],
        "observed_order_count": 50, "observed_fill_count": 40,
        "validation_start": start, "validation_end": end,
        "heldout_validation": {"status": "PASS", "sample_count": 40,
                               "mean_absolute_spread_error_bps": 0.2,
                               "mean_absolute_slippage_error_bps": 0.4},
    }
    signed, keys = _sign(doc)
    args = dict(venue="binance", market_type="spot", symbol="BTC/USDT", model_fingerprint="a" * 64)
    assert validate_execution_cost_calibration(signed, keys, **args)["calibration_verified"] is False
    doc["synthetic_only"] = False
    signed, keys = _sign(doc)
    # Even signed venue data is not accepted until reviewed limits are configured.
    missing_limits = validate_execution_cost_calibration(signed, keys, **args)
    assert missing_limits["calibration_verified"] is False
    assert missing_limits["reason"] == "COST_CALIBRATION_ACCEPTANCE_THRESHOLDS_MISSING"
    bounded_args = {**args, "max_spread_error_bps": 1.0, "max_slippage_error_bps": 1.0}
    accepted = validate_execution_cost_calibration(signed, keys, **bounded_args)
    assert accepted["calibration_verified"] is True
    assert accepted["heldout_spread_error_bps"] == 0.2
    too_tight = validate_execution_cost_calibration(
        signed, keys, **{**args, "max_spread_error_bps": 0.1, "max_slippage_error_bps": 1.0}
    )
    assert too_tight["calibration_verified"] is False
    assert too_tight["reason"] == "COST_CALIBRATION_ERROR_EXCEEDS_ACCEPTANCE_THRESHOLDS"
    assert validate_execution_cost_calibration(
        signed, keys, **{**bounded_args, "model_fingerprint": "b" * 64}
    )["calibration_verified"] is False


def test_trial_archive_must_cover_current_trials_and_a_single_oos_sample():
    dataset = hashlib.sha256(b"dataset").hexdigest()
    sample = hashlib.sha256(b"sample").hexdigest()
    protocol = "atlas-purged-walk-forward-v1"
    rows = []
    for idx in range(2):
        rows.append({
            "trial_label": f"trial-{idx}", "config_sha256": hashlib.sha256(f"cfg{idx}".encode()).hexdigest(),
            "code_sha256": hashlib.sha256(f"code{idx}".encode()).hexdigest(), "dataset_sha256": dataset,
            "oos_sample_sha256": sample, "protocol_version": protocol,
            "annualized_sharpe": 0.3 + idx / 10, "results_sha256": hashlib.sha256(f"result{idx}".encode()).hexdigest(),
            "asset_class": "crypto", "venue": "binance", "market_type": "spot",
            "symbol": "BTC/USDT", "timeframe": "1h",
        })
    document = {"evidence_type": "comparable_trial_performance_archive", "evidence_id": "trial-archive-v1",
                "archive_complete": True, "includes_all_trials": True, "trial_count": 2, "trials": rows}
    signed, keys = _sign(document)
    context = {"asset_class": "crypto", "venue": "binance", "market_type": "spot",
               "symbol": "BTC/USDT", "timeframe": "1h", "dataset_sha256": dataset,
               "oos_sample_sha256": sample, "protocol_version": protocol}
    good = validate_comparable_trial_archive(signed, keys, candidate_labels={"trial-0", "trial-1"},
                                             expected_context=context)
    assert good["trial_archive_verified"] is True
    assert validate_comparable_trial_archive(signed, keys, candidate_labels={"trial-0", "not-in-archive"},
                                              expected_context=context)["trial_archive_verified"] is False
    changed_rows = [dict(rows[0]), dict(rows[1], oos_sample_sha256=hashlib.sha256(b"other").hexdigest())]
    changed, keys = _sign({**document, "trials": changed_rows})
    assert validate_comparable_trial_archive(changed, keys, candidate_labels={"trial-0", "trial-1"},
                                              expected_context=context)["trial_archive_verified"] is False


def test_empty_or_untrusted_keyring_never_verifies():
    start, end = _window()
    document = {"evidence_type": "crypto_point_in_time_universe", "evidence_id": "x"}
    signed, _ = _sign(document)
    result = validate_crypto_universe_manifest(
        signed, {}, venue="binance", market_type="spot", coverage_start=start, coverage_end=end)
    assert result["universe_verified"] is False
    assert result["reason"] == "EVIDENCE_SIGNING_KEY_UNTRUSTED"


def test_market_request_bounds_signed_evidence_and_attaches_without_mutating_source():
    import pandas as pd
    import pytest
    from pydantic import ValidationError

    from app.schemas import MarketRequest, ResearchMarketRequest
    from app.main import _attach_research_evidence

    assert "point_in_time_universe_manifest" not in MarketRequest.model_fields
    assert "execution_cost_calibration_evidence" not in MarketRequest.model_fields
    assert "trial_performance_archive" not in MarketRequest.model_fields

    manifest = {
        "evidence_type": "crypto_point_in_time_universe",
        "evidence_id": "signed-fixture-only",
        "attestation": {"algorithm": "Ed25519", "key_id": "fixture", "signature_base64": "not-trusted"},
    }
    request = ResearchMarketRequest(point_in_time_universe_manifest=manifest)
    source = pd.DataFrame(
        {"open": [10.0], "high": [11.0], "low": [9.0], "close": [10.0]},
        index=pd.DatetimeIndex(["2026-01-01T00:00:00Z"]),
    )
    source.attrs["data_provenance"] = {"exchange": "fixture", "sha256": "a" * 64}
    attached = _attach_research_evidence(source, request)
    assert attached.attrs["point_in_time_universe_manifest"] == manifest
    assert attached.attrs["data_provenance"] == source.attrs["data_provenance"]
    assert "point_in_time_universe_manifest" not in source.attrs

    with pytest.raises(ValidationError, match="byte limit"):
        ResearchMarketRequest(point_in_time_universe_manifest={"payload": "x" * (5 * 1024 * 1024)})


def test_research_evidence_source_uris_reject_fixtures_and_local_placeholders():
    from app.research_evidence import _sources_ok

    required = {"listing_history", "delisting_history", "failed_assets"}
    valid = [_source(kind) for kind in required]
    assert _sources_ok(valid, required)

    for bad_uri in (
        "fixture://listing_history/source",
        "mock://listing_history/source",
        "file:///tmp/listings.csv",
        "https://localhost/listings.csv",
        "https://provider.invalid/listings.csv",
        "https://provider.test/listings.csv",
        "https://example.org/listings.csv",
    ):
        sources = [
            {
                "source_type": kind,
                "uri": bad_uri if kind == "listing_history" else _source(kind)["uri"],
                "sha256": hashlib.sha256(kind.encode()).hexdigest(),
            }
            for kind in required
        ]
        assert _sources_ok(sources, required) is False, bad_uri


def test_crypto_universe_rejects_malformed_delisting_timestamp():
    start, end = _window()
    instruments = [
        {"symbol": "BTC/USDT", "listed_at": "2017-01-01T00:00:00Z", "delisted_at": None},
        {"symbol": "OLD/USDT", "listed_at": "2020-01-01T00:00:00Z",
         "delisted_at": "not-a-timestamp"},
    ]
    doc = {
        "evidence_type": "crypto_point_in_time_universe", "evidence_id": "test-invalid-delisting-v1",
        "venue": "binance", "market_type": "spot", "coverage_start": start, "coverage_end": end,
        "membership_policy": "POINT_IN_TIME", "coverage_complete": True,
        "includes_delisted": True, "includes_failed_assets": True,
        "sources": [_source(k) for k in ("listing_history", "delisting_history", "failed_assets")],
        "instruments": instruments,
        "instruments_sha256": canonical_sha256(sorted(instruments, key=lambda row: row["symbol"].casefold())),
    }
    signed, keys = _sign(doc)
    result = validate_crypto_universe_manifest(
        signed, keys, venue="binance", market_type="spot", coverage_start=start,
        coverage_end=end, symbol="BTC/USDT",
    )
    assert result["universe_verified"] is False
    assert result["reason"] == "UNIVERSE_INSTRUMENT_TIMELINE_INVALID"

    failure_before_listing = [dict(row) for row in instruments]
    failure_before_listing[1]["failed_at"] = "2019-12-31T00:00:00Z"
    invalid_timeline_doc = {
        **doc,
        "instruments": failure_before_listing,
        "instruments_sha256": canonical_sha256(sorted(
            failure_before_listing, key=lambda row: row["symbol"].casefold()
        )),
    }
    signed_invalid_timeline, keys = _sign(invalid_timeline_doc)
    result = validate_crypto_universe_manifest(
        signed_invalid_timeline, keys, venue="binance", market_type="spot",
        coverage_start=start, coverage_end=end,
    )
    assert result["universe_verified"] is False
    assert result["reason"] == "UNIVERSE_INSTRUMENT_TIMELINE_INVALID"



def test_signed_universe_requires_per_instrument_failure_timeline():
    start, end = _window()
    instruments = [
        {"symbol": "BTC/USDT", "listed_at": "2017-01-01T00:00:00Z",
         "delisted_at": None, "failed_at": None},
        {"symbol": "FAILED/USDT", "listed_at": "2020-01-01T00:00:00Z",
         "delisted_at": None, "failed_at": "2024-03-01T00:00:00Z"},
    ]
    doc = {
        "evidence_type": "crypto_point_in_time_universe", "evidence_id": "test-universe-failures-v1",
        "venue": "binance", "market_type": "spot", "coverage_start": start, "coverage_end": end,
        "membership_policy": "POINT_IN_TIME", "coverage_complete": True,
        "includes_delisted": True, "includes_failed_assets": True,
        "sources": [_source(k) for k in ("listing_history", "delisting_history", "failed_assets")],
        "instruments": instruments,
        "instruments_sha256": canonical_sha256(sorted(instruments, key=lambda row: row["symbol"].casefold())),
    }
    signed, keys = _sign(doc)
    args = dict(venue="binance", market_type="spot", coverage_start=start, coverage_end=end)
    assert validate_crypto_universe_manifest(signed, keys, **args)["universe_verified"] is True

    missing_failure_timeline = [dict(row) for row in instruments]
    missing_failure_timeline[1].pop("failed_at")
    invalid_doc = {
        **doc,
        "instruments": missing_failure_timeline,
        "instruments_sha256": canonical_sha256(sorted(missing_failure_timeline, key=lambda row: row["symbol"].casefold())),
    }
    signed_invalid, keys = _sign(invalid_doc)
    result = validate_crypto_universe_manifest(signed_invalid, keys, **args)
    assert result["universe_verified"] is False
    assert result["reason"] == "UNIVERSE_INSTRUMENT_TIMELINE_INVALID"
