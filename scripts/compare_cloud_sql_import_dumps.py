#!/usr/bin/env python3
"""Compare prepared PostgreSQL dumps while normalizing only pg_dump guard keys."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

_RESTRICT = re.compile(rb"^\\restrict[ \t]+([^ \t\r\n]+)([ \t]*\r?\n?)$")
_UNRESTRICT = re.compile(rb"^\\unrestrict[ \t]+([^ \t\r\n]+)([ \t]*\r?\n?)$")
_COPY_FROM_STDIN = re.compile(rb"^\s*COPY\b.*\bFROM\s+stdin\b.*;\s*$", re.IGNORECASE)
_CANONICAL_KEY = b"ATLAS_CANONICAL_PSQL_RESTRICT_KEY"


def _command_line(line: bytes, command: bytes) -> bool:
    prefix = b"\\" + command
    return line.startswith(prefix) and (
        len(line) == len(prefix) or line[len(prefix):len(prefix) + 1] in (b" ", b"\t", b"\r", b"\n")
    )


def canonical_dump_sha256(path: str | Path) -> tuple[str, bool]:
    """Hash SQL except the values in one balanced outer pg_dump guard pair.

    PostgreSQL 17+ places these commands after the dump header and at the end
    of the SQL file. COPY FROM STDIN payload rows are treated as opaque data,
    so a row that happens to look like a psql guard is never normalized.
    """
    source = Path(path)
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("SQL dump is missing or empty")

    digest = hashlib.sha256()
    restrict_tokens: list[bytes] = []
    unrestrict_tokens: list[bytes] = []
    copy_data = False

    with source.open("rb") as handle:
        for line in handle:
            if copy_data:
                digest.update(line)
                if line.strip() == b"\\.":
                    copy_data = False
                continue

            if _command_line(line, b"restrict"):
                match = _RESTRICT.fullmatch(line)
                if match is None:
                    raise ValueError("SQL dump has a malformed pg_dump restrict guard")
                restrict_tokens.append(match.group(1))
                digest.update(b"\\restrict " + _CANONICAL_KEY + match.group(2))
                continue

            if _command_line(line, b"unrestrict"):
                match = _UNRESTRICT.fullmatch(line)
                if match is None:
                    raise ValueError("SQL dump has a malformed pg_dump unrestrict guard")
                unrestrict_tokens.append(match.group(1))
                digest.update(b"\\unrestrict " + _CANONICAL_KEY + match.group(2))
                continue

            digest.update(line)
            if _COPY_FROM_STDIN.fullmatch(line):
                copy_data = True

    if copy_data:
        raise ValueError("SQL dump COPY FROM STDIN section has no terminator")

    if not restrict_tokens and not unrestrict_tokens:
        return digest.hexdigest(), False
    if bool(restrict_tokens) != bool(unrestrict_tokens):
        raise ValueError("SQL dump has an unbalanced pg_dump restrict/unrestrict guard pair")
    if len(restrict_tokens) != 1 or len(unrestrict_tokens) != 1:
        raise ValueError("SQL dump must have exactly one pg_dump restrict/unrestrict guard pair")
    if restrict_tokens[0] != unrestrict_tokens[0]:
        raise ValueError("SQL dump pg_dump restrict/unrestrict guard keys do not match")

    return digest.hexdigest(), True


def compare_dumps(expected_path: str | Path, actual_path: str | Path) -> dict[str, object]:
    expected_hash, expected_has_guard = canonical_dump_sha256(expected_path)
    actual_hash, actual_has_guard = canonical_dump_sha256(actual_path)
    if expected_hash != actual_hash:
        raise ValueError("immutable import SQL differs from the canonicalized conversion of the pinned source")
    if expected_has_guard != actual_has_guard:
        raise ValueError("source and import SQL use different pg_dump guard-wrapper formats")
    return {
        "status": "PASS",
        "canonical_sha256": expected_hash,
        "pg_dump_guard_keys_normalized": expected_has_guard,
        "only_guard_key_values_normalized": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--actual", required=True)
    args = parser.parse_args()
    try:
        result = compare_dumps(args.expected, args.actual)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": "CLOUD_SQL_IMPORT_PROVENANCE_MISMATCH",
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
