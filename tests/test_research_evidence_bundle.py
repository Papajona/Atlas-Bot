import hashlib

from scripts.verify_research_evidence_bundle import verify_source_artifacts


def _source(root, source_type, source_map):
    uri = f"https://evidence.atlas-data.net/{source_type}/raw.csv"
    payload = f"raw source bytes for {source_type}\n".encode()
    (root / f"{source_type}.csv").write_bytes(payload)
    source_map[uri] = f"{source_type}.csv"
    return {"source_type": source_type, "uri": uri, "sha256": hashlib.sha256(payload).hexdigest()}


def _documents(root, source_map):
    universe = [_source(root, kind, source_map) for kind in
                ("listing_history", "delisting_history", "failed_assets")]
    calibration = [_source(root, kind, source_map) for kind in
                   ("observed_fills", "order_book", "fee_schedule")]
    archive = [_source(root, kind, source_map) for kind in
               ("experiment_log", "config_snapshot", "code_snapshot", "dataset", "oos_results")]
    return {
        "universe": {"sources": universe},
        "calibration": {"sources": calibration},
        "trial_archive": {"sources": archive},
    }


def test_bundle_verifier_matches_each_source_uri_to_raw_bytes(tmp_path):
    source_map = {}
    docs = _documents(tmp_path, source_map)
    result = verify_source_artifacts(docs, tmp_path, source_map)
    assert result["verified"] is True
    assert result["checked_count"] == 11
    assert result["issues"] == []


def test_bundle_verifier_detects_changed_raw_source_bytes(tmp_path):
    source_map = {}
    docs = _documents(tmp_path, source_map)
    uri = docs["calibration"]["sources"][0]["uri"]
    (tmp_path / source_map[uri]).write_bytes(b"replaced after signing")
    result = verify_source_artifacts(docs, tmp_path, source_map)
    assert result["verified"] is False
    assert any(issue["code"] == "SOURCE_ARTIFACT_HASH_MISMATCH" for issue in result["issues"])


def test_bundle_verifier_rejects_path_traversal_and_missing_mappings(tmp_path):
    source_map = {}
    docs = _documents(tmp_path, source_map)
    uri = docs["universe"]["sources"][0]["uri"]
    source_map[uri] = "../outside.csv"
    del source_map[docs["calibration"]["sources"][0]["uri"]]
    result = verify_source_artifacts(docs, tmp_path, source_map)
    codes = {issue["code"] for issue in result["issues"]}
    assert result["verified"] is False
    assert "SOURCE_MAP_PATH_UNSAFE" in codes
    assert "SOURCE_ARTIFACT_PATH_UNSAFE" in codes or "SOURCE_ARTIFACT_PATH_ESCAPE_OR_MISSING" in codes
    assert "SOURCE_ARTIFACT_NOT_MAPPED" in codes


def test_bundle_verifier_rejects_archive_without_raw_reconciliation_artifacts(tmp_path):
    source_map = {}
    docs = _documents(tmp_path, source_map)
    docs["trial_archive"]["sources"] = docs["trial_archive"]["sources"][:2]
    source_map = {
        source["uri"]: source_map[source["uri"]]
        for name in docs
        for source in docs[name]["sources"]
    }
    result = verify_source_artifacts(docs, tmp_path, source_map)
    assert result["verified"] is False
    assert any(
        issue["code"] == "SOURCE_TYPES_MISSING"
        and issue["detail"].startswith("trial_archive:")
        for issue in result["issues"]
    )
