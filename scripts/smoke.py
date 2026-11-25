#!/usr/bin/env python3
from __future__ import annotations

import http.client
import ipaddress
import ssl
import sys
from urllib.parse import urlsplit


def validate_base_url(value: str) -> str:
    normalized = str(value or "").strip()
    parsed = urlsplit(normalized)
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("base URL must contain a host and must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base URL must not contain a query string or fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("base URL contains an invalid port") from exc
    if parsed.scheme == "https":
        return normalized.rstrip("/")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if loopback:
            return normalized.rstrip("/")
    raise ValueError("base URL must use HTTPS, or HTTP on loopback only")


def _connection(parsed):
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port if parsed.port is not None else default_port
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(
            parsed.hostname,
            port=port,
            timeout=5,
            context=ssl.create_default_context(),
        )
    return http.client.HTTPConnection(parsed.hostname, port=port, timeout=5)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    base = validate_base_url(args[0] if args else "http://127.0.0.1:8000")
    parsed = urlsplit(base)
    prefix = parsed.path.rstrip("/")
    for path in ("/healthz", "/readyz", "/metrics"):
        connection = _connection(parsed)
        try:
            connection.request("GET", prefix + path)
            response = connection.getresponse()
            body = response.read(200).decode(errors="replace")
            if 300 <= response.status < 400:
                raise RuntimeError(
                    f"{path} returned an unexpected redirect ({response.status})"
                )
            if not 200 <= response.status < 300:
                raise RuntimeError(f"{path} returned HTTP {response.status}")
            print(path, response.status, body)
        finally:
            # Each response gets its own connection so a bounded read cannot leave unread
            # response bytes on a keep-alive socket before the next endpoint request.
            connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
