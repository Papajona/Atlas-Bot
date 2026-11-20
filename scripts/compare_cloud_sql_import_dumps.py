#!/usr/bin/env python3
"""Compare prepared PostgreSQL dumps, normalizing only the outer pg_dump guard pair."""
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
        len(line) == len(prefix)
        or line[len(prefix):len(prefix) + 1] in (b" ", b"\t", b"\r", b"\n")
    )


def _is_header_or_trailer_comment(line: bytes) -> bool:
    """Recognize only blank and -- lines outside the generated SQL statements."""
    stripped = line.lstrip()
    return not stripped or stripped.startswith(b"--")


def _outer_guard(line: bytes, command: bytes, pattern: re.Pattern[bytes]) -> tuple[bytes, bytes] | None:
    if not _command_line(line, command):
        return None
    match = pattern.fullmatch(line)
    if match is None:
        raise ValueError(f"SQL dump has a malformed outer pg_dump {command.decode()} guard")
    return match.group(1), match.group(2)


def _outer_guard_lines(path: Path) -> tuple[int | None, bytes | None, int | None, bytes | None]:
    """Find guards only in the dump preamble and epilogue, never inside SQL.

    pg_dump's generated script may start with several -- header comments,
    followed by a restrict command; its final executable line is the matching
    unrestrict command, sometimes followed by footer comments. SQL literals,
    function bodies, comments inside statements and COPY payloads are interior
    bytes and cannot be classified as wrapper commands by this boundary check.
    """
    first_index: int | None = None
    first_line: bytes | None = None
    last_index: int | None = None
    last_line: bytes | None = None
    with path.open("rb") as handle:
        for index, line in enumerate(handle):
            if not _is_header_or_trailer_comment(line):
                if first_index is None:
                    first_index, first_line = index, line
                last_index, last_line = index, line

    if first_index is None or last_index is None:
        return None, None, None, None

    restrict = _outer_guard(first_line or b"", b"restrict", _RESTRICT)
    unrestrict = _outer_guard(last_line or b"", b"unrestrict", _UNRESTRICT)

    if (restrict is None) != (unrestrict is None):
        raise ValueError("SQL dump has an unbalanced outer pg_dump restrict/unrestrict guard pair")

    if restrict is not None and unrestrict is not None:
        if restrict[0] != unrestrict[0]:
            raise ValueError("SQL dump outer pg_dump restrict/unrestrict guard keys do not match")
        return first_index, restrict[0], last_index, unrestrict[0]

    return None, None, None, None


def canonical_dump_sha256(path: str | Path) -> tuple[str, bool]:
    """Hash all SQL bytes, except the random keys of one validated outer guard pair."""
    source = Path(path)
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("SQL dump is missing or empty")

    first_index, _restrict_key, last_index, _unrestrict_key = _outer_guard_lines(source)
    has_guard = first_index is not None and last_index is not None
    digest = hashlib.sha256()

    with source.open("rb") as handle:
        for index, line in enumerate(handle):
            if has_guard and index == first_index:
                match = _RESTRICT.fullmatch(line)
                if match is None:
                    raise ValueError("SQL dump outer pg_dump restrict guard changed during hashing")
                digest.update(b"\\restrict " + _CANONICAL_KEY + match.group(2))
            elif has_guard and index == last_index:
                match = _UNRESTRICT.fullmatch(line)
                if match is None:
                    raise ValueError("SQL dump outer pg_dump unrestrict guard changed during hashing")
                digest.update(b"\\unrestrict " + _CANONICAL_KEY + match.group(2))
            else:
                # Every interior line is included verbatim, including guard-looking
                # text in SQL literals, comments and COPY data.
                digest.update(line)

    return digest.hexdigest(), has_guard


def fingerprint_dump(path: str | Path) -> dict[str, object]:
    """Report the raw and canonical SHA-256 for an already-prepared source SQL file."""
    source = Path(path)
    canonical_hash, has_guard = canonical_dump_sha256(source)
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "status": "PASS",
        "source_prepared_sql_bytes": source.stat().st_size,
        "source_prepared_sql_sha256": digest.hexdigest(),
        "canonical_sha256": canonical_hash,
        "pg_dump_guard_keys_normalized": has_guard,
        "only_outer_guard_key_values_normalized": True,
    }


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
        "only_outer_guard_key_values_normalized": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fingerprint",
        metavar="PREPARED_SQL",
        help="report raw and canonical SHA-256 for a source SQL file already prepared with pinned PostgreSQL tooling",
    )
    parser.add_argument("--expected", help="expected prepared SQL file for byte-equivalence comparison")
    parser.add_argument("--actual", help="actual prepared SQL file for byte-equivalence comparison")
    args = parser.parse_args()

    if args.fingerprint:
        if args.expected or args.actual:
            parser.error("--fingerprint cannot be combined with --expected or --actual")
    elif not args.expected or not args.actual:
        parser.error("provide --fingerprint PREPARED_SQL or both --expected and --actual")

    try:
        result = (
            fingerprint_dump(args.fingerprint)
            if args.fingerprint
            else compare_dumps(args.expected, args.actual)
        )
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
