#!/usr/bin/env python3
from __future__ import annotations

import ipaddress
import sys
from urllib.parse import urlsplit

import httpx


def validate_base_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("base URL must contain a host and must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base URL must not contain a query string or fragment")
    if parsed.scheme == "https":
        return value.rstrip("/")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if loopback:
            return value.rstrip("/")
    raise ValueError("base URL must use HTTPS, or HTTP on loopback only")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    base = validate_base_url(args[0] if args else "http://127.0.0.1:8000")
    with httpx.Client(timeout=5.0, follow_redirects=False) as client:
        for path in ("/healthz", "/readyz", "/metrics"):
            response = client.get(base + path)
            if 300 <= response.status_code < 400:
                raise RuntimeError(f"{path} returned an unexpected redirect ({response.status_code})")
            response.raise_for_status()
            print(path, response.status_code, response.text[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
