#!/usr/bin/env python3
"""Normalize libpq-style PostgreSQL URLs for SQLAlchemy's asyncpg driver."""

from __future__ import annotations

import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_ASYNCPG_SCHEME_BY_LEGACY = {
    "postgresql": "postgresql+asyncpg",
    "postgres": "postgresql+asyncpg",
}
_ALLOWED_SSL_MODES = {
    "disable",
    "allow",
    "prefer",
    "require",
    "verify-ca",
    "verify-full",
}


def normalize_asyncpg_database_url(database_url: str) -> str:
    """Normalize legacy driver schemes and libpq sslmode for asyncpg.

    asyncpg accepts the same SSL mode values through its ssl connection
    argument, while sslmode is a libpq option and is not recognized by
    asyncpg. Reject ambiguous or unsupported combinations rather than
    silently weakening or changing the requested TLS behavior.
    """
    if not isinstance(database_url, str) or not database_url.strip():
        raise ValueError("DATABASE_URL must be a non-empty PostgreSQL URL")

    parsed = urlsplit(database_url.strip())
    normalized_scheme = parsed.scheme.lower()
    scheme = _ASYNCPG_SCHEME_BY_LEGACY.get(normalized_scheme, normalized_scheme)
    if scheme != "postgresql+asyncpg":
        raise ValueError("DATABASE_URL must use PostgreSQL with the asyncpg driver")

    query = parse_qsl(parsed.query, keep_blank_values=True)
    sslmode_indexes = [i for i, (key, _) in enumerate(query) if key.lower() == "sslmode"]
    ssl_indexes = [i for i, (key, _) in enumerate(query) if key.lower() == "ssl"]

    if len(sslmode_indexes) > 1:
        raise ValueError("DATABASE_URL must not contain repeated sslmode parameters")
    if sslmode_indexes:
        if ssl_indexes:
            raise ValueError("DATABASE_URL must not combine sslmode and ssl parameters")
        index = sslmode_indexes[0]
        mode = query[index][1].lower()
        if mode not in _ALLOWED_SSL_MODES:
            raise ValueError("DATABASE_URL contains an unsupported sslmode")
        query[index] = ("ssl", mode)

    return urlunsplit(
        (scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), parsed.fragment)
    )


def main() -> int:
    if len(sys.argv) != 1:
        print("Usage: normalize_asyncpg_database_url.py < DATABASE_URL", file=sys.stderr)
        return 2
    try:
        database_url = sys.stdin.read()
        print(normalize_asyncpg_database_url(database_url))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
