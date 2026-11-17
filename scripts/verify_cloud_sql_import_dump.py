#!/usr/bin/env python3
"""Lex plain/gzip PostgreSQL SQL and reject commands that change import databases."""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path
from typing import BinaryIO


_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_PSQL_CONNECT = re.compile(r"\\(connect|c)\b", re.IGNORECASE)


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


def _database_ddl(tokens: list[str]) -> bool:
    words = [token.upper() for token in tokens]
    if len(words) >= 2 and words[0] in {"ALTER", "DROP"} and words[1] == "DATABASE":
        return True
    if len(words) >= 2 and words[0] == "CREATE" and words[1] == "DATABASE":
        return True
    if len(words) >= 3 and words[0] == "CREATE" and words[1] in {"TEMP", "TEMPORARY"} and words[2] == "DATABASE":
        return True
    return False


def _copy_from_stdin(tokens: list[str]) -> bool:
    words = [token.upper() for token in tokens]
    return words and words[0] == "COPY" and any(
        words[index:index + 2] == ["FROM", "STDIN"]
        for index in range(len(words) - 1)
    )


def _inspect_lines(lines) -> tuple[int, int]:
    state = "normal"
    block_depth = 0
    dollar_delimiter = ""
    statement_tokens: list[str] = []
    word: list[str] = []
    copy_pending = False
    copy_data = False
    total_bytes = 0
    line_count = 0

    def flush_word() -> None:
        if word:
            statement_tokens.append("".join(word))
            word.clear()

    for raw_line in lines:
        line_count += 1
        total_bytes += len(raw_line)
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Cloud SQL import dump is not UTF-8 SQL text") from exc
        if line_count == 1:
            line = line.lstrip("\ufeff")

        if copy_data:
            if line.strip() == r"\.":
                copy_data = False
            continue
        if copy_pending:
            copy_pending = False
            copy_data = True
            if line.strip() == r"\.":
                copy_data = False
            continue

        i = 0
        while i < len(line):
            char = line[i]
            nxt = line[i + 1] if i + 1 < len(line) else ""

            if state == "line_comment":
                break
            if state == "block_comment":
                if char == "/" and nxt == "*":
                    block_depth += 1
                    i += 2
                    continue
                if char == "*" and nxt == "/":
                    block_depth -= 1
                    i += 2
                    if block_depth == 0:
                        state = "normal"
                    continue
                i += 1
                continue
            if state == "single_quote":
                if char == "\\":
                    i += 2
                    continue
                if char == "'" and nxt == "'":
                    i += 2
                    continue
                if char == "'":
                    state = "normal"
                i += 1
                continue
            if state == "double_quote":
                if char == '"' and nxt == '"':
                    i += 2
                    continue
                if char == '"':
                    state = "normal"
                i += 1
                continue
            if state == "dollar_quote":
                if line.startswith(dollar_delimiter, i):
                    i += len(dollar_delimiter)
                    state = "normal"
                else:
                    i += 1
                continue

            # Normal SQL/psql input. Comments and quoted content are not executable
            # command tokens and must not cause valid backup data to be rejected.
            if char == "-" and nxt == "-":
                flush_word()
                state = "line_comment"
                break
            if char == "/" and nxt == "*":
                flush_word()
                state = "block_comment"
                block_depth = 1
                i += 2
                continue
            if char == "'":
                flush_word()
                state = "single_quote"
                i += 1
                continue
            if char == '"':
                flush_word()
                state = "double_quote"
                i += 1
                continue
            if char == "$":
                match = _DOLLAR_QUOTE.match(line, i)
                if match:
                    flush_word()
                    dollar_delimiter = match.group(0)
                    state = "dollar_quote"
                    i = match.end()
                    continue

            if char == "\\":
                flush_word()
                if _PSQL_CONNECT.match(line, i):
                    raise ValueError(
                        "Cloud SQL import dump contains a psql \\connect/\\c database switch"
                    )
                i += 1
                continue

            if char.isalnum() or char == "_":
                word.append(char)
                i += 1
                continue

            if char == ";":
                flush_word()
                if _database_ddl(statement_tokens):
                    raise ValueError(
                        "Cloud SQL import dump contains executable CREATE/ALTER/DROP DATABASE"
                    )
                if _copy_from_stdin(statement_tokens):
                    copy_pending = True
                statement_tokens.clear()
                i += 1
                continue

            flush_word()
            i += 1

        if state == "line_comment":
            state = "normal"
        # SQL keywords are whitespace-delimited even when a statement spans lines.
        flush_word()

    flush_word()
    if state in {"single_quote", "double_quote", "block_comment", "dollar_quote"}:
        raise ValueError("Cloud SQL import dump ends inside an unterminated SQL lexical construct")
    if _database_ddl(statement_tokens):
        raise ValueError("Cloud SQL import dump ends with executable CREATE/ALTER/DROP DATABASE")
    if copy_data or copy_pending:
        raise ValueError("Cloud SQL COPY FROM STDIN section is missing its \\. terminator")
    return total_bytes, line_count


def inspect_import_dump(path: str | Path) -> dict:
    source = Path(path)
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("Cloud SQL import dump is missing or empty")
    stream, compressed = _open_sql(source)
    try:
        total_bytes, line_count = _inspect_lines(stream)
    finally:
        stream.close()
    if total_bytes <= 0:
        raise ValueError("Cloud SQL import dump decompressed to an empty stream")
    return {
        "status": "PASS",
        "format": "gzip_sql" if compressed else "plain_sql",
        "compressed": compressed,
        "decompressed_bytes": total_bytes,
        "line_count": line_count,
        "database_switch_commands": 0,
        "database_ddl_commands": 0,
        "target_database_is_not_overridden_by_dump": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = inspect_import_dump(args.input)
        Path(args.output).write_text(
            json.dumps(result, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": "CLOUD_SQL_IMPORT_DUMP_TARGET_OVERRIDE",
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
