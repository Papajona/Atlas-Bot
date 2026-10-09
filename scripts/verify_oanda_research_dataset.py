#!/usr/bin/env python3
"""Verify the integrity and internal consistency of an OANDA research capture."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

try:
    from .capture_oanda_research_dataset import export_completed_candles
except ImportError:  # Direct execution as `python scripts/verify_oanda_research_dataset.py`
    from capture_oanda_research_dataset import export_completed_candles


def verify_dataset(dataset_dir: Path) -> dict[str, Any]:
    raw_path = dataset_dir / "oanda_raw_response.json"
    csv_path = dataset_dir / "oanda_completed_mid_candles.csv"
    manifest_path = dataset_dir / "manifest.json"
    for path in (raw_path, csv_path, manifest_path):
        if not path.is_file():
            raise ValueError(f"Required dataset file is missing: {path.name}")

    raw_bytes = raw_path.read_bytes()
    csv_bytes = csv_path.read_bytes()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if hashlib.sha256(raw_bytes).hexdigest() != manifest.get("raw_response_sha256"):
        raise ValueError("Raw response SHA-256 does not match manifest")
    if hashlib.sha256(csv_bytes).hexdigest() != manifest.get("normalized_csv_sha256"):
        raise ValueError("Normalized CSV SHA-256 does not match manifest")

    payload = json.loads(raw_bytes)
    if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
        raise ValueError("Raw response does not contain a candles array")
    expected_csv, timestamps = export_completed_candles(payload)
    if expected_csv != csv_bytes:
        raise ValueError("CSV does not match the completed candles in the raw response")
    if len(timestamps) != manifest.get("completed_row_count"):
        raise ValueError("Completed row count does not match manifest")
    if len(payload["candles"]) != manifest.get("response_candle_count_including_incomplete"):
        raise ValueError("Raw response candle count does not match manifest")
    if timestamps[0] != manifest.get("first_completed_timestamp_utc"):
        raise ValueError("First timestamp does not match manifest")
    if timestamps[-1] != manifest.get("last_completed_timestamp_utc"):
        raise ValueError("Last timestamp does not match manifest")

    return {
        "status": "verified",
        "instrument": manifest.get("instrument"),
        "granularity": manifest.get("granularity"),
        "completed_rows": len(timestamps),
        "first_timestamp_utc": timestamps[0],
        "last_timestamp_utc": timestamps[-1],
        "raw_response_sha256": manifest["raw_response_sha256"],
        "normalized_csv_sha256": manifest["normalized_csv_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", default="oanda_dataset_capture")
    args = parser.parse_args()
    try:
        print(json.dumps(verify_dataset(Path(args.dataset_dir)), indent=2))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
