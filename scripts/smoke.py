#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import ipaddress
import ssl
from pathlib import Path
from urllib.parse import urlsplit


_READINESS_PURPOSE = b"atlas-paper-staging-readiness-v1"


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


def _read_secret_file(path: Path, label: str) -> str:
    """Read an operator-provided secret without echoing its contents or path."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise ValueError(f"{label} file could not be read") from None
    if not value:
        raise ValueError(f"{label} file is empty")
    return value


def paper_readiness_token_from_file(path: Path) -> str:
    """Derive the same purpose-bound token used by app.paper_staging /readyz."""
    database_url = _read_secret_file(path, "paper database URL")
    return hmac.new(
        database_url.encode("utf-8"),
        _READINESS_PURPOSE,
        hashlib.sha256,
    ).hexdigest()


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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Atlas liveness, authenticated readiness, and (optionally) metrics."
    )
    parser.add_argument(
        "base_url",
        nargs="?",
        default="http://127.0.0.1:8000",
        help="service base URL (HTTPS, or loopback HTTP only)",
    )
    parser.add_argument(
        "--paper-database-url-file",
        type=Path,
        help="protected file containing CUSTOMER_PAPER_DATABASE_URL; derives the paper /readyz Bearer token",
    )
    parser.add_argument(
        "--admin-token-file",
        type=Path,
        help="protected file containing the admin Bearer token used by /metrics",
    )
    parser.add_argument(
        "--skip-metrics",
        action="store_true",
        help="skip /metrics (required for the restricted paper-only ASGI entrypoint)",
    )
    args = parser.parse_args(argv)
    if args.skip_metrics and args.admin_token_file is not None:
        parser.error("--admin-token-file cannot be combined with --skip-metrics")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        base = validate_base_url(args.base_url)
        readiness_token = (
            paper_readiness_token_from_file(args.paper_database_url_file)
            if args.paper_database_url_file is not None
            else None
        )
        admin_token = (
            _read_secret_file(args.admin_token_file, "admin token")
            if args.admin_token_file is not None
            else None
        )
    except ValueError as exc:
        raise SystemExit(f"BLOCKED: {exc}") from None

    parsed = urlsplit(base)
    prefix = parsed.path.rstrip("/")
    checks: list[tuple[str, str | None]] = [
        ("/healthz", None),
        ("/readyz", readiness_token),
    ]
    if not args.skip_metrics:
        checks.append(("/metrics", admin_token))

    for path, bearer_token in checks:
        connection = _connection(parsed)
        try:
            headers = (
                {"Authorization": f"Bearer {bearer_token}"}
                if bearer_token is not None
                else {}
            )
            connection.request("GET", prefix + path, headers=headers)
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
