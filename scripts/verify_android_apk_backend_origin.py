#!/usr/bin/env python3
"""Fail-closed evidence check for the backend origin embedded in a signed Android APK.

The check inspects executable code for MainActivity's reference to the exact URL that
Gradle receives for BuildConfig.BACKEND_URL, canonicalizes its origin, and optionally compares it with
the origin intended for deployment. It never trusts release metadata as proof that the
APK contains the requested URL.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import struct
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit


_NON_PRODUCTION_HOST = re.compile(
    r"(^|[.-])(dev|development|staging|test|sandbox|localhost)([.-]|$)",
    re.IGNORECASE,
)


def normalize_backend_url(value: str) -> tuple[str, str]:
    """Return (Gradle-normalized URL literal, canonical HTTPS origin)."""
    if not isinstance(value, str) or not value:
        raise ValueError("backend URL is empty")
    if any(ord(char) < 0x20 or char in {'"', "\\", "\r", "\n"} for char in value):
        raise ValueError("backend URL contains invalid control or quoting characters")

    # Match app/build.gradle.kts: trim surrounding whitespace and trailing root slashes.
    literal = value.strip().rstrip("/")
    if not literal:
        raise ValueError("backend URL normalizes to an empty value")
    try:
        parsed = urlsplit(literal)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("backend URL has an invalid authority or port") from exc

    if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname:
        raise ValueError("backend URL must be an absolute HTTPS origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("backend URL must not contain credentials")
    if "?" in literal or "#" in literal or parsed.path not in ("", "/"):
        raise ValueError("backend URL must be an origin without a path, query, or fragment")
    if port not in (None, 443):
        raise ValueError("backend URL may use only the default HTTPS port")

    host = parsed.hostname.lower().rstrip(".")
    if not host or host.startswith("[") or ":" in host:
        raise ValueError("backend URL must use an approved DNS hostname, not an IP literal")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("backend URL must use a DNS hostname, not an IP literal")

    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("backend URL hostname is invalid") from exc
    labels = ascii_host.split(".")
    if len(labels) < 2 or any(
        not label
        or len(label) > 63
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ):
        raise ValueError("backend URL must contain a valid public DNS hostname")
    if _NON_PRODUCTION_HOST.search(ascii_host):
        raise ValueError("backend URL hostname indicates a non-production environment")
    if ascii_host in {"example.com", "example.org", "example.net"} or ascii_host.endswith(
        (".invalid", ".local", ".internal")
    ):
        raise ValueError("backend URL uses a reserved or example hostname")

    origin = f"https://{ascii_host}"
    return literal, origin


def _read_uleb128(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    for _ in range(5):
        if offset >= len(data):
            raise ValueError("truncated DEX ULEB128 value")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
    raise ValueError("invalid DEX ULEB128 value")


def dex_strings(data: bytes) -> list[str]:
    """Read DEX string_ids with bounds checks; URLs are ASCII in DEX MUTF-8."""
    if len(data) < 0x70 or not data.startswith(b"dex\n") or data[7:8] != b"\0":
        raise ValueError("APK contains an invalid or unsupported DEX file")
    size, offset = struct.unpack_from("<II", data, 0x38)
    if size > 1_000_000 or offset > len(data) or size * 4 > len(data) - offset:
        raise ValueError("DEX string table is outside file bounds")

    result: list[str] = []
    for index in range(size):
        (string_offset,) = struct.unpack_from("<I", data, offset + index * 4)
        if string_offset >= len(data):
            raise ValueError("DEX string data offset is outside file bounds")
        _, cursor = _read_uleb128(data, string_offset)
        end = data.find(b"\0", cursor)
        if end < 0:
            raise ValueError("DEX string is not null terminated")
        result.append(data[cursor:end].decode("utf-8", errors="replace"))
    return result


def _class_uses_string(data: bytes, class_def_offset: int, string_index: int, strings: list[str]) -> bool:
    """Return whether a class' executable code contains a const-string reference."""
    class_data_offset = struct.unpack_from("<I", data, class_def_offset + 24)[0]
    if not class_data_offset:
        return False
    cursor = class_data_offset
    counts = []
    for _ in range(4):
        value, cursor = _read_uleb128(data, cursor)
        counts.append(value)
    method_count = counts[2] + counts[3]
    method_index = 0
    for _ in range(counts[0] + counts[1]):
        _, cursor = _read_uleb128(data, cursor)
        _, cursor = _read_uleb128(data, cursor)
    for _ in range(method_count):
        delta, cursor = _read_uleb128(data, cursor)
        _, cursor = _read_uleb128(data, cursor)
        code_offset, cursor = _read_uleb128(data, cursor)
        method_index += delta
        if not code_offset:
            continue
        if code_offset + 16 > len(data):
            raise ValueError("DEX method code is outside file bounds")
        instruction_count = struct.unpack_from("<I", data, code_offset + 12)[0]
        instruction_end = code_offset + 16 + instruction_count * 2
        if instruction_end > len(data):
            raise ValueError("DEX method instructions are outside file bounds")
        for position in range(code_offset + 16, instruction_end, 2):
            opcode = data[position] & 0xff
            if opcode == 0x1A:
                referenced = struct.unpack_from("<H", data, position + 2)[0]
                if referenced == string_index:
                    return True
            elif opcode == 0x1B:
                referenced = struct.unpack_from("<I", data, position + 2)[0]
                if referenced == string_index:
                    return True
    return False


def apk_uses_backend_url(apk_path: str | Path, literal: str) -> bool:
    """Check that MainActivity executable code, rather than another DEX string, uses URL."""
    try:
        with zipfile.ZipFile(apk_path) as apk:
            dex_names = sorted(
                name for name in apk.namelist()
                if re.fullmatch(r"classes(?:[0-9]+)?\.dex", name)
            )
            if not dex_names:
                raise ValueError("APK has no classes*.dex files")
            for name in dex_names:
                data = apk.read(name)
                strings = dex_strings(data)
                try:
                    string_index = strings.index(literal)
                except ValueError:
                    continue
                if len(data) < 0x70:
                    continue
                class_count, class_offset = struct.unpack_from("<II", data, 0x60)
                if class_offset + class_count * 32 > len(data):
                    raise ValueError("DEX class definitions are outside file bounds")
                string_ids_offset = struct.unpack_from("<I", data, 0x3c)[0]
                type_ids_offset = struct.unpack_from("<I", data, 0x44)[0]
                for index in range(class_count):
                    class_def = class_offset + index * 32
                    class_index = struct.unpack_from("<I", data, class_def)[0]
                    if type_ids_offset + class_index * 4 + 4 > len(data):
                        raise ValueError("DEX type definition is outside file bounds")
                    descriptor_index = struct.unpack_from(
                        "<I", data, type_ids_offset + class_index * 4
                    )[0]
                    if string_ids_offset + descriptor_index * 4 + 4 > len(data):
                        raise ValueError("DEX class descriptor is outside file bounds")
                    descriptor_offset = struct.unpack_from(
                        "<I", data, string_ids_offset + descriptor_index * 4
                    )[0]
                    _, descriptor_cursor = _read_uleb128(data, descriptor_offset)
                    end = data.find(b"\\0", descriptor_cursor)
                    descriptor = data[descriptor_cursor:end].decode("utf-8", errors="replace")
                    if descriptor.endswith("/MainActivity;") and _class_uses_string(
                        data, class_def, string_index, strings
                    ):
                        return True
            return False
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("APK could not be read as a valid ZIP archive") from exc


def verify_apk_backend_origin(
    apk_path: str | Path,
    expected_url: str,
    compare_origin: str | None = None,
) -> dict[str, str]:
    """Prove MainActivity executable code uses the configured URL literal."""
    literal, origin = normalize_backend_url(expected_url)
    if not apk_uses_backend_url(apk_path, literal):
        raise ValueError(
            "the expected backend URL is not referenced by MainActivity executable code"
        )
    if compare_origin is not None:
        _, intended_origin = normalize_backend_url(compare_origin)
        if intended_origin != origin:
            raise ValueError(
                f"APK backend origin mismatch: artifact={origin}, intended={intended_origin}"
            )
    return {"backend_url": literal, "backend_origin": origin}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", required=True, help="Signed APK to inspect")
    parser.add_argument(
        "--expected-url",
        required=True,
        help="URL supplied to Gradle (or the corresponding value recorded in release evidence)",
    )
    parser.add_argument(
        "--compare-origin",
        help="Intended production base URL; its canonical origin must match the APK",
    )
    args = parser.parse_args()
    try:
        evidence = verify_apk_backend_origin(
            args.apk, args.expected_url, args.compare_origin
        )
    except ValueError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
