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
_CANONICAL_KEY = b"ATLAS_CANONICAL_PSQL_RESTRICT_KEY"


def _command_line(line: bytes, command: bytes) -> bool:
    prefix = b"\\" + command
    return line.startswith(prefix) and (
        len(line) == len(prefix) or line[len(prefix):len(prefix) + 1] in (b" ", b"\t", b"\r", b"\n")
    )


def canonical_dump_sha256(path: str | Path) -> tuple[str, bool]:
    """Hash SQL except for one validated outer pg_dump guard-key pair.

    PostgreSQL 17+ pg_dump emits a random restrict key on the first line
    and a matching unrestrict key on the last line. Only those key values
    are normalized. Interior SQL/COPY data is never searched or rewritten.
    """
    source = Path(path)
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("SQL dump is missing or empty")

    digest = hashlib.sha256()
    with source.open("rb") as handle:
        first = handle.readline()
        first_match = _RESTRICT.fullmatch(first)
        if _command_line(first, b"restrict") and first_match is None:
            raise ValueError("SQL dump has a malformed leading pg_dump restrict guard")
        if first_match:
            first_token = first_match.group(1)
            digest.update(b"\\restrict " + _CANONICAL_KEY + first_match.group(2))
        else:
            first_token = None
            digest.update(first)

        previous = None
        for line in handle:
            if previous is not None:
                digest.update(previous)
            previous = line

    if previous is None:
        if first_token is not None:
            raise ValueError("SQL dump has an unbalanced pg_dump restrict guard")
        return digest.hexdigest(), False

    last_match = _UNRESTRICT.fullmatch(previous)
    if _command_line(previous, b"unrestrict") and last_match is None:
        raise ValueError("SQL dump has a malformed trailing pg_dump unrestrict guard")

    if first_token is None and last_match is not None:
        raise ValueError("SQL dump has an unbalanced pg_dump unrestrict guard")
    if first_token is not None and last_match is None:
        raise ValueError("SQL dump has an unbalanced pg_dump restrict guard")
    if first_token is not None and last_match is not None:
        if first_token != last_match.group(1):
            raise ValueError("SQL dump pg_dump restrict/unrestrict guard keys do not match")
        digest.update(b"\\unrestrict " + _CANONICAL_KEY + last_match.group(2))
        has_guard = True
    else:
        digest.update(previous)
        has_guard = False

    return digest.hexdigest(), has_guard


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
