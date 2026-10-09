from __future__ import annotations

import csv
import hashlib
import json
from io import StringIO

import pytest

from scripts.capture_oanda_research_dataset import export_completed_candles, parse_utc


def candle(time: str, *, complete: bool = True) -> dict:
    return {
        "time": time,
        "complete": complete,
        "volume": 12,
        "mid": {"o": "1.10001", "h": "1.10123", "l": "1.09987", "c": "1.10009"},
    }


def test_export_keeps_provider_decimal_strings_and_only_completed_candles():
    payload = {
        "candles": [
            candle("2025-01-01T00:00:00.000000000Z"),
            candle("2025-01-01T01:00:00.000000000Z", complete=False),
            candle("2025-01-01T02:00:00.000000000Z"),
        ]
    }
    encoded, timestamps = export_completed_candles(payload)
    rows = list(csv.DictReader(StringIO(encoded.decode("utf-8"))))
    assert len(rows) == 2
    assert rows[0]["close"] == "1.10009"
    assert rows[0]["time"] == "2025-01-01T00:00:00Z"
    assert timestamps == ["2025-01-01T00:00:00Z", "2025-01-01T02:00:00Z"]


def test_export_rejects_duplicate_completed_timestamps():
    with pytest.raises(ValueError, match="duplicate"):
        export_completed_candles({"candles": [
            candle("2025-01-01T00:00:00Z"),
            candle("2025-01-01T00:00:00.000000000Z"),
        ]})


def test_export_rejects_out_of_order_completed_timestamps():
    with pytest.raises(ValueError, match="not ascending"):
        export_completed_candles({"candles": [
            candle("2025-01-01T01:00:00Z"),
            candle("2025-01-01T00:00:00Z"),
        ]})


def test_timestamp_requires_timezone():
    with pytest.raises(ValueError, match="timezone"):
        parse_utc("2025-01-01T00:00:00")



def test_export_preserves_nanosecond_timestamp_precision():
    payload = {"candles": [
        candle("2025-01-01T00:00:00.123456789Z"),
        candle("2025-01-01T00:00:00.123456790Z"),
    ]}
    encoded, timestamps = export_completed_candles(payload)
    rows = list(csv.DictReader(StringIO(encoded.decode("utf-8"))))
    assert rows[0]["time"] == "2025-01-01T00:00:00.123456789Z"
    assert rows[1]["time"] == "2025-01-01T00:00:00.123456790Z"
    assert timestamps == [
        "2025-01-01T00:00:00.123456789Z",
        "2025-01-01T00:00:00.12345679Z",
    ]


def test_timestamp_ordering_keeps_submicrosecond_order():
    from scripts.capture_oanda_research_dataset import timestamp_order_key

    first = timestamp_order_key("2025-01-01T00:00:00.123456789Z")
    second = timestamp_order_key("2025-01-01T00:00:00.123456790Z")
    assert first < second


def test_timestamp_normalization_handles_timezone_offsets():
    from scripts.capture_oanda_research_dataset import normalize_timestamp_utc

    assert normalize_timestamp_utc("2025-01-01T01:00:00.250000+01:00") == (
        "2025-01-01T00:00:00.25Z"
    )



def test_verifier_accepts_consistent_capture_and_rejects_tampering(tmp_path):
    from scripts.capture_oanda_research_dataset import export_completed_candles
    from scripts.verify_oanda_research_dataset import verify_dataset

    payload = {"candles": [
        candle("2025-01-01T00:00:00.123456789Z"),
        candle("2025-01-01T01:00:00.000000000Z"),
    ]}
    raw_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    csv_bytes, timestamps = export_completed_candles(payload)
    (tmp_path / "oanda_raw_response.json").write_bytes(raw_bytes)
    (tmp_path / "oanda_completed_mid_candles.csv").write_bytes(csv_bytes)
    manifest = {
        "raw_response_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "normalized_csv_sha256": hashlib.sha256(csv_bytes).hexdigest(),
        "completed_row_count": len(timestamps),
        "response_candle_count_including_incomplete": len(payload["candles"]),
        "first_completed_timestamp_utc": timestamps[0],
        "last_completed_timestamp_utc": timestamps[-1],
        "instrument": "EUR_USD",
        "granularity": "H1",
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert verify_dataset(tmp_path)["status"] == "verified"
    (tmp_path / "oanda_completed_mid_candles.csv").write_bytes(csv_bytes + b"# tampered\\n")
    with pytest.raises(ValueError, match="CSV SHA-256"):
        verify_dataset(tmp_path)
