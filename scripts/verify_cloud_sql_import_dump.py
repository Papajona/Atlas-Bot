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


_WORD_LIMIT = 64
_LITERAL_SAMPLE_LIMIT = 128
_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]{0,62})?\$")
_PSQL_CONNECT = re.compile(r"\\(connect|c)\b", re.IGNORECASE)
_FALSE_VALUES = {"OFF", "FALSE", "NO", "0", "DISABLE"}


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


def _is_database_ddl(prefix: list[str]) -> bool:
    if len(prefix) >= 2 and prefix[0] in {"ALTER", "DROP"} and prefix[1] == "DATABASE":
        return True
    if len(prefix) >= 2 and prefix[0] == "CREATE" and prefix[1] == "DATABASE":
        return True
    return len(prefix) >= 3 and prefix[0] == "CREATE" and prefix[1] in {
        "TEMP", "TEMPORARY",
    } and prefix[2] == "DATABASE"


def _inspect_lines(lines) -> tuple[int, int]:
    """Scan with bounded statement/token state; never retain a whole SQL statement."""
    state = "normal"
    block_depth = 0
    dollar_delimiter = ""
    escape_single_quote = False
    word: list[str] = []
    word_overflow = False
    word_overflow_marker = "__OTHER__"
    statement_prefix: list[str] = []
    previous_token = ""
    copy_from_stdin = False
    statement_has_setting_name = False
    statement_has_off_token = False
    statement_has_set_config = False
    literal_mentions_setting = False
    literal_is_off = False
    literal_sample: list[str] = []
    identifier_sample: list[str] = []
    copy_pending = False
    copy_data = False
    total_bytes = 0
    line_count = 0

    def add_token(token: str) -> None:
        nonlocal previous_token, copy_from_stdin
        nonlocal statement_has_setting_name, statement_has_off_token
        nonlocal statement_has_set_config
        upper = token.upper()
        if len(statement_prefix) < 3:
            statement_prefix.append(upper)
        if upper == "STANDARD_CONFORMING_STRINGS":
            statement_has_setting_name = True
        if upper in _FALSE_VALUES:
            statement_has_off_token = True
        if upper == "SET_CONFIG":
            statement_has_set_config = True
        if statement_prefix and statement_prefix[0] == "COPY":
            if previous_token == "FROM" and upper == "STDIN":
                copy_from_stdin = True
        previous_token = upper

    def flush_word() -> None:
        nonlocal word_overflow
        if word or word_overflow:
            token = "".join(word) if not word_overflow else word_overflow_marker
            add_token(token)
            word.clear()
            word_overflow = False

    def append_word_char(char: str) -> None:
        nonlocal word_overflow
        if len(word) < _WORD_LIMIT:
            word.append(char)
        else:
            word_overflow = True

    def reset_statement() -> None:
        nonlocal previous_token, copy_from_stdin
        nonlocal statement_has_setting_name, statement_has_off_token
        nonlocal statement_has_set_config, literal_mentions_setting, literal_is_off
        statement_prefix.clear()
        previous_token = ""
        copy_from_stdin = False
        statement_has_setting_name = False
        statement_has_off_token = False
        statement_has_set_config = False
        literal_mentions_setting = False
        literal_is_off = False

    def finish_statement() -> bool:
        nonlocal copy_pending
        first_two = statement_prefix[:2]
        changes_string_mode = (
            (
                statement_has_setting_name
                and (
                    (bool(statement_prefix) and statement_prefix[0] == "SET"
                     and (statement_has_off_token or literal_is_off))
                    or (first_two == ["ALTER", "SYSTEM"]
                        and (statement_has_off_token or literal_is_off))
                )
            )
            or (
                statement_has_set_config
                and literal_mentions_setting
                and literal_is_off
            )
        )
        if changes_string_mode:
            raise ValueError(
                "Cloud SQL import dump changes standard_conforming_strings; "
                "the SQL string boundaries cannot be certified"
            )
        if _is_database_ddl(statement_prefix):
            raise ValueError(
                "Cloud SQL import dump contains executable CREATE/ALTER/DROP DATABASE"
            )
        if statement_prefix:
            copy_pending = bool(statement_prefix[0] == "COPY" and copy_from_stdin)
        reset_statement()
        return copy_pending

    def record_literal() -> None:
        nonlocal literal_mentions_setting, literal_is_off
        sample = "".join(literal_sample).strip().lower()
        if "standard_conforming_strings" in sample:
            literal_mentions_setting = True
        if sample in {"off", "false", "no", "0", "disable"}:
            literal_is_off = True
        literal_sample.clear()

    def append_literal_char(char: str) -> None:
        if len(literal_sample) < _LITERAL_SAMPLE_LIMIT:
            literal_sample.append(char)

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
                if escape_single_quote and char == "\\":
                    append_literal_char(char)
                    if i + 1 < len(line):
                        append_literal_char(line[i + 1])
                    i += 2
                    continue
                if char == "'" and nxt == "'":
                    append_literal_char("'")
                    i += 2
                    continue
                if char == "'":
                    record_literal()
                    state = "normal"
                    escape_single_quote = False
                else:
                    append_literal_char(char)
                i += 1
                continue
            if state == "double_quote":
                if char == '"' and nxt == '"':
                    if len(identifier_sample) < _LITERAL_SAMPLE_LIMIT:
                        identifier_sample.append('"')
                    i += 2
                    continue
                if char == '"':
                    identifier = "".join(identifier_sample).upper()
                    if identifier == "STANDARD_CONFORMING_STRINGS":
                        statement_has_setting_name = True
                    identifier_sample.clear()
                    add_token("__QUOTED_IDENTIFIER__")
                    state = "normal"
                    i += 1
                    continue
                if len(identifier_sample) < _LITERAL_SAMPLE_LIMIT:
                    identifier_sample.append(char)
                i += 1
                continue
            if state == "dollar_quote":
                if line.startswith(dollar_delimiter, i):
                    i += len(dollar_delimiter)
                    dollar_delimiter = ""
                    add_token("__QUOTED_LITERAL__")
                    state = "normal"
                else:
                    i += 1
                continue

            # Normal SQL/psql input. Ignore comments and literal contents, but
            # preserve enough bounded token state to detect executable commands.
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

            # PostgreSQL U&'...' Unicode escape strings use backslash escapes.
            if (
                char == "&"
                and not word_overflow
                and "".join(word).upper() == "U"
                and nxt == "'"
            ):
                word.clear()
                state = "single_quote"
                escape_single_quote = True
                literal_sample.clear()
                i += 2
                continue
            if char == "'":
                prefix = "".join(word).upper() if not word_overflow else ""
                if prefix in {"E", "N", "B", "X"}:
                    word.clear()
                    word_overflow = False
                else:
                    flush_word()
                state = "single_quote"
                escape_single_quote = prefix == "E"
                literal_sample.clear()
                i += 1
                continue
            if char == '"':
                flush_word()
                state = "double_quote"
                identifier_sample.clear()
                i += 1
                continue

            if char == "\\":
                flush_word()
                if _PSQL_CONNECT.match(line, i):
                    raise ValueError(
                        "Cloud SQL import dump contains a psql \\connect/\\c database switch"
                    )
                i += 1
                continue

            if char == "$":
                # PostgreSQL permits dollar signs inside unquoted identifiers.
                # Only recognize a dollar-quote opener at a token boundary.
                if word or word_overflow:
                    append_word_char(char)
                    i += 1
                    continue
                match = _DOLLAR_QUOTE.match(line, i)
                if match:
                    dollar_delimiter = match.group(0)
                    state = "dollar_quote"
                    i = match.end()
                    continue
                i += 1
                continue

            if char.isalnum() or char == "_":
                append_word_char(char)
                i += 1
                continue

            if char == ";":
                flush_word()
                finish_statement()
                i += 1
                continue

            flush_word()
            i += 1

        if state == "line_comment":
            state = "normal"
        # Flushing at a line boundary preserves bounded memory while retaining
        # multi-line SQL keyword recognition (CREATE newline DATABASE).
        flush_word()

    flush_word()
    if state in {"single_quote", "double_quote", "block_comment", "dollar_quote"}:
        raise ValueError("Cloud SQL import dump ends inside an unterminated SQL lexical construct")
    finish_statement()
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
