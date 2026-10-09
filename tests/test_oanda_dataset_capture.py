from __future__ import annotations

import csv
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
