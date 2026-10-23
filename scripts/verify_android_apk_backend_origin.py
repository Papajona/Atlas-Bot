#!/usr/bin/env python3
"""Verify the effective Android runtime backend string resource in a built APK.

MainActivity obtains its API/portal base URL from R.string.atlas_backend_url at
runtime. This verifier asks Android's aapt resource-table decoder for that exact
resource symbol. It intentionally does not search DEX-wide strings, because an
unused approved URL string can coexist with a different active backend.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


_NON_PRODUCTION_HOST = re.compile(
    r"(^|[.-])(dev|development|staging|test|sandbox|localhost)([.-]|$)",
    re.IGNORECASE,
)
_RESOURCE_HEADER = re.compile(
    r"^\s*resource\s+0x[0-9a-fA-F]+\s+.*\bstring/atlas_backend_url(?::.*)?\s*$"
)
_NEXT_RESOURCE = re.compile(r"^\s*resource\s+0x[0-9a-fA-F]+\s+")
_STRING_VALUE = re.compile(r'\((?:string8|string16)\)\s+"([^"\r\n]*)"\s*$')


def normalize_backend_url(value: str) -> tuple[str, str]:
    """Return (normalized URL string, canonical HTTPS origin)."""
    if not isinstance(value, str) or not value:
        raise ValueError("backend URL is empty")
    if any(ord(char) < 0x20 or char in {'"', "\\", "\r", "\n"} for char in value):
        raise ValueError("backend URL contains invalid control or quoting characters")

    # Match app/build.gradle.kts and MainActivity: trim whitespace and trailing slashes.
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
        raise ValueError("backend URL must use a DNS hostname, not an IP literal")
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

    return literal, f"https://{ascii_host}"


def extract_runtime_backend_url_from_resource_dump(resource_dump: str) -> str:
    """Extract only the value of the resource that MainActivity reads at runtime."""
    lines = resource_dump.splitlines()
    headers = [index for index, line in enumerate(lines) if _RESOURCE_HEADER.fullmatch(line)]
    if len(headers) != 1:
        raise ValueError(
            "APK must expose exactly one resources-table entry for string/atlas_backend_url"
        )

    start = headers[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _NEXT_RESOURCE.match(lines[index]):
            end = index
            break

    values: list[str] = []
    for line in lines[start:end]:
        match = _STRING_VALUE.search(line.strip())
        if match:
            values.append(match.group(1))

    # Resource configurations may repeat a value, but divergent configurations would
    # make the runtime choice device-dependent and cannot be approved by one origin.
    distinct_values = set(values)
    if not distinct_values:
        raise ValueError("runtime string/atlas_backend_url has no decoded string value")
    if len(distinct_values) != 1:
        raise ValueError("runtime string/atlas_backend_url has ambiguous configuration values")
    return distinct_values.pop()


def verify_runtime_backend_origin(
    resource_dump: str,
    expected_url: str,
    compare_origin: str | None = None,
) -> dict[str, str]:
    """Verify actual resource value, expected build input, and optional intended origin."""
    expected_literal, expected_origin = normalize_backend_url(expected_url)
    actual_url = extract_runtime_backend_url_from_resource_dump(resource_dump)
    actual_literal, actual_origin = normalize_backend_url(actual_url)

    if actual_literal != expected_literal:
        raise ValueError(
            "effective runtime backend resource does not match the expected release URL"
        )
    if actual_origin != expected_origin:
        raise ValueError("effective runtime backend resource origin does not match expected origin")

    if compare_origin is not None:
        _, intended_origin = normalize_backend_url(compare_origin)
        if intended_origin != actual_origin:
            raise ValueError(
                f"APK runtime backend origin mismatch: artifact={actual_origin}, intended={intended_origin}"
            )

    return {"backend_url": actual_literal, "backend_origin": actual_origin}


def verify_apk_backend_origin(
    apk_path: str | Path,
    expected_url: str,
    compare_origin: str | None = None,
    aapt_path: str | Path = "aapt",
) -> dict[str, str]:
    """Decode resources.arsc and verify the exact runtime resource before release/deploy."""
    try:
        result = subprocess.run(
            [str(aapt_path), "dump", "--values", "resources", str(apk_path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise ValueError(f"could not execute Android aapt resource-table decoder: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip()
        raise ValueError(
            "aapt could not decode the APK resource table"
            + (f": {detail}" if detail else "")
        )
    return verify_runtime_backend_origin(result.stdout, expected_url, compare_origin)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", required=True, help="APK to inspect")
    parser.add_argument("--aapt", required=True, help="Android SDK aapt executable")
    parser.add_argument(
        "--expected-url",
        required=True,
        help="Backend URL supplied to Gradle and recorded in release evidence",
    )
    parser.add_argument(
        "--compare-origin",
        help="Intended production base URL; its canonical origin must match the runtime resource",
    )
    args = parser.parse_args()
    try:
        evidence = verify_apk_backend_origin(
            args.apk, args.expected_url, args.compare_origin, args.aapt
        )
    except ValueError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main)
