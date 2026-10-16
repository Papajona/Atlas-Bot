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
    return {"source_type": kind, "uri": f"fixture://{kind}/source",
            "sha256": hashlib.sha256(kind.encode()).hexdigest()}


def _window():
    return ("2024-01-01T00:00:00Z", "2025-01-01T00:00:00Z")


def test_signed_point_in_time_universe_verifies_and_rejects_forgery():
    start, end = _window()
    instruments = [
        {"symbol": "BTC/USDT", "listed_at": "2017-01-01T00:00:00Z", "delisted_at": None},
        {"symbol": "OLD/USDT", "listed_at": "2020-01-01T00:00:00Z",
         "delisted_at": "2024-06-01T00:00:00Z"},
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
    assert validate_execution_cost_calibration(signed, keys, **args)["calibration_verified"] is True
    assert validate_execution_cost_calibration(signed, keys, **{**args, "model_fingerprint": "b" * 64})["calibration_verified"] is False


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
