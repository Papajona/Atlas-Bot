#!/usr/bin/env python3
"""Capture a fixed-window OANDA v20 PRACTICE candle dataset with provenance.

This script is read-only: it calls the instrument candles endpoint and never places
orders. It preserves the exact HTTP response body, exports completed mid candles,
and writes a SHA-256 manifest. It deliberately requires a fixed UTC date window.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
from decimal import Decimal
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

PRACTICE_BASE_URL = "https://api-fxpractice.oanda.com"
GRANULARITIES = {
    "S5", "S10", "S15", "S30",
    "M1", "M2", "M4", "M5", "M10", "M15", "M30",
    "H1", "H2", "H3", "H4", "H6", "H8", "H12", "D", "W",
    "M",
}
CSV_FIELDS = ["time", "volume", "open", "high", "low", "close"]


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp with an explicit timezone and return UTC."""
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must include a timezone (use Z for UTC): {value!r}")
    return parsed.astimezone(timezone.utc)


_TIMESTAMP_RE = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d+))?(?P<zone>Z|[+-]\d{2}:\d{2})$"
)


def normalize_timestamp_utc(value: str) -> str:
    """Normalize ISO-8601 timestamps to UTC without discarding fractional seconds."""
    match = _TIMESTAMP_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Invalid ISO-8601 timestamp: {value!r}")
    zone = "+00:00" if match.group("zone") == "Z" else match.group("zone")
    base = datetime.fromisoformat(match.group("base") + zone).astimezone(timezone.utc)
    fraction = (match.group("fraction") or "").rstrip("0")
    normalized = base.strftime("%Y-%m-%dT%H:%M:%S")
    if fraction:
        normalized += "." + fraction
    return normalized + "Z"


def timestamp_order_key(value: str) -> tuple[datetime, Decimal]:
    normalized = normalize_timestamp_utc(value)
    match = _TIMESTAMP_RE.fullmatch(normalized)
    assert match is not None
    base = datetime.fromisoformat(match.group("base") + "+00:00")
    fraction = Decimal("0." + match.group("fraction")) if match.group("fraction") else Decimal(0)
    return base, fraction


def canonical_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def export_completed_candles(payload: dict[str, Any]) -> tuple[bytes, list[str]]:
    candles = payload.get("candles")
    if not isinstance(candles, list):
        raise ValueError("OANDA response has no candles array")
    rows: list[list[str]] = []
    timestamps: list[str] = []
    for candle in candles:
        if not isinstance(candle, dict):
            raise ValueError("OANDA response contains a malformed candle")
        if candle.get("complete") is not True:
            continue
        mid = candle.get("mid")
        if not isinstance(mid, dict) or any(k not in mid for k in ("o", "h", "l", "c")):
            raise ValueError("Completed OANDA candle is missing mid OHLC values")
        timestamp = candle.get("time")
        if not isinstance(timestamp, str):
            raise ValueError("Completed OANDA candle is missing its timestamp")
        normalized_timestamp = normalize_timestamp_utc(timestamp)
        timestamps.append(normalized_timestamp)
        # Preserve the provider's exact timestamp text and decimal strings in the CSV.
        # The normalized UTC form is used only for ordering and duplicate checks.
        rows.append([
            timestamp,
            str(candle.get("volume", "")),
            str(mid["o"]),
            str(mid["h"]),
            str(mid["l"]),
            str(mid["c"]),
        ])
    if not rows:
        raise ValueError("OANDA returned no completed candles in the requested window")
    timestamp_keys = [timestamp_order_key(value) for value in timestamps]
    if len(timestamp_keys) != len(set(timestamp_keys)):
        raise ValueError("OANDA response contains duplicate completed-candle timestamps")
    if timestamp_keys != sorted(timestamp_keys):
        raise ValueError("OANDA response completed-candle timestamps are not ascending")
    from io import StringIO
    stream = StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(CSV_FIELDS)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8"), timestamps


def capture(
    *,
    instrument: str,
    granularity: str,
    start: str,
    end: str,
    output_dir: Path,
    expected_rows: int | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    token = token or os.environ.get("OANDA_API_TOKEN", "")
    if not token:
        raise RuntimeError("OANDA_API_TOKEN is missing; configure it as a GitHub Actions secret.")
    instrument = instrument.strip().upper()
    granularity = granularity.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]+_[A-Z0-9]+", instrument):
        raise ValueError("Instrument must use OANDA format such as EUR_USD.")
    if granularity not in GRANULARITIES:
        raise ValueError(f"Unsupported OANDA granularity: {granularity}")
    parse_utc(start)
    parse_utc(end)
    if timestamp_order_key(normalize_timestamp_utc(start)) >= timestamp_order_key(normalize_timestamp_utc(end)):
        raise ValueError("from_utc must be earlier than to_utc.")

    request_params = {
        "from": normalize_timestamp_utc(start),
        "to": normalize_timestamp_utc(end),
        "granularity": granularity,
        "price": "M",
        "smooth": "false",
        "includeFirst": "true",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "oanda_raw_response.json"
    csv_path = output_dir / "oanda_completed_mid_candles.csv"
    manifest_path = output_dir / "manifest.json"
    url = f"{PRACTICE_BASE_URL}/v3/instruments/{instrument}/candles"

    with httpx.Client(timeout=30.0, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }) as client:
        response = client.get(url, params=request_params)
    raw_bytes = response.content
    raw_path.write_bytes(raw_bytes)
    if response.status_code >= 400:
        raise RuntimeError(
            f"OANDA practice API returned HTTP {response.status_code}; "
            f"raw error body saved to {raw_path}."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("OANDA returned invalid JSON; raw response was preserved.") from exc

    csv_bytes, completed_times = export_completed_candles(payload)
    csv_path.write_bytes(csv_bytes)
    fetched_at = datetime.now(timezone.utc).isoformat()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "provider": "OANDA v20",
        "environment": "practice",
        "base_url": PRACTICE_BASE_URL,
        "endpoint": f"/v3/instruments/{instrument}/candles",
        "instrument": instrument,
        "granularity": granularity,
        "price_component": "M",
        "request_parameters": request_params,
        "retrieved_at_utc": fetched_at,
        "http_status": response.status_code,
        "response_candle_count_including_incomplete": len(payload.get("candles", [])),
        "completed_row_count": len(completed_times),
        "first_completed_timestamp_utc": completed_times[0],
        "last_completed_timestamp_utc": completed_times[-1],
        "raw_response_file": raw_path.name,
        "raw_response_sha256": sha256_bytes(raw_bytes),
        "normalized_csv_file": csv_path.name,
        "normalized_csv_sha256": sha256_bytes(csv_bytes),
        "expected_completed_rows": expected_rows,
        "row_count_matches_expectation": (
            None if expected_rows is None else len(completed_times) == expected_rows
        ),
        "timestamp_note": "CSV contains completed mid candles only; provider OHLC decimal strings are preserved.",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "instrument": instrument,
        "granularity": granularity,
        "completed_rows": len(completed_times),
        "first_timestamp_utc": completed_times[0],
        "last_timestamp_utc": completed_times[-1],
        "raw_response_sha256": manifest["raw_response_sha256"],
        "normalized_csv_sha256": manifest["normalized_csv_sha256"],
        "manifest": str(manifest_path),
    }, indent=2))
    if expected_rows is not None and len(completed_times) != expected_rows:
        raise RuntimeError(
            f"Expected {expected_rows} completed rows but captured {len(completed_times)}. "
            "Artifacts were written for inspection; do not treat this as the original dataset."
        )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument", required=True, help="OANDA instrument, e.g. EUR_USD")
    parser.add_argument("--granularity", required=True, help="OANDA granularity, e.g. H1")
    parser.add_argument("--from-utc", required=True, help="ISO-8601 timestamp with timezone")
    parser.add_argument("--to-utc", required=True, help="ISO-8601 timestamp with timezone")
    parser.add_argument("--output-dir", default="oanda_dataset_capture")
    parser.add_argument("--expected-rows", type=int, default=None)
    args = parser.parse_args()
    try:
        capture(
            instrument=args.instrument,
            granularity=args.granularity,
            start=args.from_utc,
            end=args.to_utc,
            output_dir=Path(args.output_dir),
            expected_rows=args.expected_rows,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
