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
            if _PSQL_CONNECT.search(line):
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
