"""Fail closed unless release verification targets its disposable local database."""
from __future__ import annotations

import ipaddress
import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


def is_approved_verification_database_url(
    database_url: str,
    *,
    compose_service_host: str = "",
    in_container: bool = False,
) -> bool:
    """Accept SQLite/loopback URLs or the declared Compose postgres service only.

    The test environment itself is not a safety boundary: this script runs Alembic
    and database-mutating concurrency tests, so a caller-supplied remote URL must
    never be accepted merely because ENVIRONMENT is set to ``test``.
    """
    value = str(database_url or "").strip()
    if not value:
        return False
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.partition("+")[0].lower()
        if scheme == "sqlite":
            return not any(
                key.lower() in {"host", "hostaddr"}
                for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
            )
        if scheme not in {"postgres", "postgresql"}:
            return False
        # Disallow driver-level host overrides: they can silently redirect a
        # loopback or Compose-looking authority to a different server.
        if any(
            key.lower() in {"host", "hostaddr"}
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        ):
            return False
        hostname = parsed.hostname
        _ = parsed.port  # Validate malformed/out-of-range explicit ports.
    except ValueError:
        return False

    if not hostname:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        pass

    # Docker Compose gives the postgres service this exact network alias.
    # Require both the explicitly configured alias and a container runtime;
    # arbitrary remote DNS names never pass this exception.
    return bool(
        normalized == "postgres"
        and str(compose_service_host or "").strip().lower() == "postgres"
        and in_container
    )


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "")
    compose_service = os.environ.get("ATLAS_VERIFY_COMPOSE_POSTGRES_SERVICE", "")
    if not is_approved_verification_database_url(
        database_url,
        compose_service_host=compose_service,
        in_container=Path("/.dockerenv").exists(),
    ):
        print(
            "Refusing release verification: DATABASE_URL must target SQLite, "
            "a loopback database, or the declared in-container Compose postgres service.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
