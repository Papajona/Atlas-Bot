from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest

from scripts.verify_android_apk_backend_origin import (
    dex_strings,
    normalize_backend_url,
    verify_apk_backend_origin,
)


def _uleb128(value: int) -> bytes:
    encoded = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            encoded.append(byte | 0x80)
        else:
            encoded.append(byte)
            return bytes(encoded)


def _synthetic_dex(strings: list[str]) -> bytes:
    """Build the minimum DEX header/string table needed to test our parser."""
    string_ids_offset = 0x70
    string_data_offset = string_ids_offset + 4 * len(strings)
    data = bytearray(0x70)
    data[:8] = b"dex\n035\0"
    struct.pack_into("<II", data, 0x38, len(strings), string_ids_offset)

    string_ids = bytearray()
    string_data = bytearray()
    for value in strings:
        string_ids.extend(struct.pack("<I", string_data_offset + len(string_data)))
        encoded = value.encode("utf-8")
        string_data.extend(_uleb128(len(value)))
        string_data.extend(encoded)
        string_data.append(0)
    return bytes(data + string_ids + string_data)


def _apk(tmp_path: Path, strings: list[str]) -> Path:
    path = tmp_path / "release.apk"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("classes.dex", _synthetic_dex(strings))
    return path


def test_normalizes_https_origin_and_gradle_url_literal():
    literal, origin = normalize_backend_url("  https://ATLAS.EXAMPLE.COM:443/  ")
    assert literal == "https://ATLAS.EXAMPLE.COM:443"
    assert origin == "https://atlas.example.com"


@pytest.mark.parametrize(
    "value",
    [
        "http://atlas.example.com",
        "https://user:pass@atlas.example.com",
        "https://atlas.example.com/api",
        "https://atlas.example.com?other=1",
        "https://atlas.example.com:8443",
        "https://localhost",
        "https://staging.atlas.example.com",
        "https://192.0.2.10",
    ],
)
def test_rejects_urls_that_are_not_approved_https_production_origins(value: str):
    with pytest.raises(ValueError):
        normalize_backend_url(value)


def test_reads_dex_string_ids_and_verifies_actual_apk_literal(tmp_path: Path):
    literal = "https://atlas.example.com"
    apk = _apk(tmp_path, ["MainActivity", literal, "another constant"])
    with zipfile.ZipFile(apk) as archive:
        strings = dex_strings(archive.read("classes.dex"))
    assert literal in strings
    assert verify_apk_backend_origin(apk, literal) == {
        "backend_url": literal,
        "backend_origin": "https://atlas.example.com",
    }


def test_rejects_artifact_without_expected_backend_url(tmp_path: Path):
    apk = _apk(tmp_path, ["MainActivity", "https://other.example.com"])
    with pytest.raises(ValueError, match="absent from"):
        verify_apk_backend_origin(apk, "https://atlas.example.com")


def test_rejects_artifact_origin_that_differs_from_intended_deployment(tmp_path: Path):
    apk = _apk(tmp_path, ["MainActivity", "https://atlas.example.com"])
    with pytest.raises(ValueError, match="origin mismatch"):
        verify_apk_backend_origin(
            apk,
            "https://atlas.example.com",
            "https://unrelated.example.com",
        )


def test_accepts_equivalent_canonical_origin_for_deployment(tmp_path: Path):
    apk = _apk(tmp_path, ["MainActivity", "https://ATLAS.EXAMPLE.COM:443"])
    evidence = verify_apk_backend_origin(
        apk,
        "https://ATLAS.EXAMPLE.COM:443/",
        "https://atlas.example.com/",
    )
    assert evidence["backend_origin"] == "https://atlas.example.com"
