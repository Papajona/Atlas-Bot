#!/usr/bin/env python3
"""Reject Cloud SQL SQL dumps that can redirect the import to another database."""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path
from typing import BinaryIO, Iterator

_DATABASE_DDL = re.compile(
    r"\b(?:CREATE|ALTER|DROP)\s+(?:[A-Z_]+\s+)*DATABASE\b",
    re.IGNORECASE,
)
_PSQL_CONNECT = re.compile(r"^\s*\\(?:connect|c)(?:\s|$)", re.IGNORECASE)
_PSQL_CONNECT_INLINE = re.compile(r"\\(?:connect|c)(?:\s|$)", re.IGNORECASE)


def _has_executable_psql_connect(line: str) -> bool:
    """Return True if a psql \\connect/\\c meta-command appears outside quotes and comments.

    psql accepts meta-commands after SQL on the same line (e.g. ``SELECT 1; \\connect db``).
    Quoted strings and comments are skipped so that literal backslashes are not flagged.
    The scan is per line, which errs on the side of flagging when quoting spans lines.
    """
    if _PSQL_CONNECT.search(line):
        return True
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch in ("'", '"'):
            # Skip the quoted section; doubled quote characters are escapes.
            i += 1
            while i < n:
                if line[i] == ch:
                    if i + 1 < n and line[i + 1] == ch:
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            continue
        if line.startswith("--", i):
            return False
        if line.startswith("/*", i):
            end = line.find("*/", i + 2)
            if end == -1:
                return False
            i = end + 2
            continue
        if ch == "\\" and _PSQL_CONNECT_INLINE.match(line, i):
            return True
        i += 1
    return False


def _open_sql(path: Path) -> tuple[BinaryIO, bool]:
    raw = path.open("rb")
    magic = raw.read(5)
    raw.seek(0)
    if magic == b"PGDMP":
        raw.close()
        raise ValueError("PostgreSQL custom-format archives are not accepted for database-target proof")
    if magic.startswith(b"\x1f\x8b"):
        raw.close()
        return gzip.open(path, "rb"), True
    if magic.startswith(b"ustar") or magic[:2] in {b"PK", b"\x1f\x9d"}:
        raw.close()
        raise ValueError("unsupported archive/compression format; provide plain SQL or gzip-compressed SQL")
    return raw, False


def inspect_import_dump(path: str | Path) -> dict:
    source = Path(path)
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("Cloud SQL import dump is missing or empty")
    stream, compressed = _open_sql(source)
    total_bytes = 0
    lines = 0
    carry = ""
    try:
        for raw_line in stream:
            total_bytes += len(raw_line)
            lines += 1
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Cloud SQL import dump is not UTF-8 SQL text") from exc
            if lines == 1:
                line = line.lstrip("\ufeff")
            if _has_executable_psql_connect(line):
                raise ValueError("Cloud SQL import dump contains a psql \\connect/\\c database switch")
            window = carry + line
            if _DATABASE_DDL.search(window):
                raise ValueError("Cloud SQL import dump contains CREATE/ALTER/DROP DATABASE and can redirect the import target")
            carry = window[-128:]
    finally:
        stream.close()
    if not total_bytes:
        raise ValueError("Cloud SQL import dump decompressed to an empty stream")
    return {
        "status": "PASS",
        "format": "gzip_sql" if compressed else "plain_sql",
        "compressed": compressed,
        "decompressed_bytes": total_bytes,
        "line_count": lines,
        "database_switch_statements": 0,
        "database_ddl_statements": 0,
        "target_database_is_not_overridden_by_dump": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = inspect_import_dump(args.input)
        Path(args.output).write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "reason": "CLOUD_SQL_IMPORT_DUMP_TARGET_OVERRIDE", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
