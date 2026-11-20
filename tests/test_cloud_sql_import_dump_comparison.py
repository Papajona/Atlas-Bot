from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.compare_cloud_sql_import_dumps import compare_dumps, fingerprint_dump


def _write(path: Path, guard_key: str, payload: str = "CREATE TABLE public.probe(id integer);\n"):
    path.write_text(
        "-- PostgreSQL database dump header\n"
        f"\\restrict {guard_key}\n"
        f"{payload}"
        f"\\unrestrict {guard_key}\n"
        "-- PostgreSQL database dump complete\n",
        encoding="utf-8",
    )


def test_random_pg_dump_guard_keys_are_the_only_normalized_bytes(tmp_path):
    source = tmp_path / "source.sql"
    uploaded = tmp_path / "uploaded.sql"
    _write(source, "first-random-key-0123456789")
    _write(uploaded, "second-random-key-9876543210")

    result = compare_dumps(source, uploaded)

    assert result["status"] == "PASS"
    assert result["pg_dump_guard_keys_normalized"] is True
    assert result["only_outer_guard_key_values_normalized"] is True


def test_import_sql_payload_mismatch_is_not_hidden_by_guard_normalization(tmp_path):
    source = tmp_path / "source.sql"
    uploaded = tmp_path / "uploaded.sql"
    _write(source, "first-key", "CREATE TABLE public.probe(id integer);\n")
    _write(uploaded, "second-key", "CREATE TABLE public.changed(id integer);\n")

    with pytest.raises(ValueError, match="differs from the canonicalized conversion"):
        compare_dumps(source, uploaded)


def test_each_dump_must_have_a_matching_restrict_and_unrestrict_pair(tmp_path):
    source = tmp_path / "source.sql"
    uploaded = tmp_path / "uploaded.sql"
    _write(source, "source-key")
    uploaded.write_text(
        "\\restrict another-key\n"
        "CREATE TABLE public.probe(id integer);\n"
        "\\unrestrict mismatched-key\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="keys do not match"):
        compare_dumps(source, uploaded)


def test_unbalanced_guard_is_rejected(tmp_path):
    source = tmp_path / "source.sql"
    uploaded = tmp_path / "uploaded.sql"
    _write(source, "source-key")
    uploaded.write_text(
        "\\restrict another-key\nCREATE TABLE public.probe(id integer);\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unbalanced"):
        compare_dumps(source, uploaded)


def test_guard_like_lines_inside_copy_data_are_not_normalized(tmp_path):
    source = tmp_path / "source.sql"
    uploaded = tmp_path / "uploaded.sql"
    payload = (
        "CREATE TABLE public.probe(value text);\n"
        "COPY public.probe (value) FROM stdin;\n"
        "\\restrict user-data-is-not-a-wrapper\n"
        "\\unrestrict user-data-is-not-a-wrapper\n"
        "\\.\n"
    )
    _write(source, "first-key", payload)
    _write(uploaded, "second-key", payload)

    result = compare_dumps(source, uploaded)
    assert result["status"] == "PASS"


def test_plain_single_line_sql_without_guards_can_be_compared(tmp_path):
    source = tmp_path / "source.sql"
    uploaded = tmp_path / "uploaded.sql"
    source.write_text("SELECT 1;", encoding="utf-8")
    uploaded.write_text("SELECT 1;", encoding="utf-8")

    assert compare_dumps(source, uploaded)["status"] == "PASS"


def test_unwrapped_sql_is_compared_byte_for_byte(tmp_path):
    source = tmp_path / "source.sql"
    uploaded = tmp_path / "uploaded.sql"
    source.write_text("CREATE TABLE public.probe(id integer);\n", encoding="utf-8")
    uploaded.write_text("CREATE TABLE public.probe(id bigint);\n", encoding="utf-8")

    with pytest.raises(ValueError, match="differs from the canonicalized conversion"):
        compare_dumps(source, uploaded)

def test_guard_like_lines_inside_dollar_quoted_function_are_hashed_verbatim(tmp_path):
    source = tmp_path / "source.sql"
    uploaded = tmp_path / "uploaded.sql"
    source_payload = (
        "CREATE FUNCTION public.guard_text() RETURNS text\n"
        "LANGUAGE sql AS $body$\n"
        "\\restrict function-literal-key-one\n"
        "\\unrestrict function-literal-key-one\n"
        "$body$;\n"
    )
    changed_payload = (
        "CREATE FUNCTION public.guard_text() RETURNS text\n"
        "LANGUAGE sql AS $body$\n"
        "\\restrict function-literal-key-two\n"
        "\\unrestrict function-literal-key-two\n"
        "$body$;\n"
    )
    _write(source, "outer-source-key", source_payload)
    _write(uploaded, "outer-upload-key", changed_payload)

    with pytest.raises(ValueError, match="differs from the canonicalized conversion"):
        compare_dumps(source, uploaded)


def test_guard_like_lines_inside_sql_block_comments_are_hashed_verbatim(tmp_path):
    source = tmp_path / "source.sql"
    uploaded = tmp_path / "uploaded.sql"
    source_payload = (
        "/* begin preserved comment\n"
        "\\restrict comment-key-one\n"
        "\\unrestrict comment-key-one\n"
        "end preserved comment */\n"
        "CREATE TABLE public.probe(id integer);\n"
    )
    changed_payload = (
        "/* begin preserved comment\n"
        "\\restrict comment-key-two\n"
        "\\unrestrict comment-key-two\n"
        "end preserved comment */\n"
        "CREATE TABLE public.probe(id integer);\n"
    )
    _write(source, "outer-source-key", source_payload)
    _write(uploaded, "outer-upload-key", changed_payload)

    with pytest.raises(ValueError, match="differs from the canonicalized conversion"):
        compare_dumps(source, uploaded)


def test_plain_sql_with_paired_guard_text_inside_function_is_not_treated_as_wrapper(tmp_path):
    source = tmp_path / "source.sql"
    uploaded = tmp_path / "uploaded.sql"
    source.write_text(
        "CREATE FUNCTION public.guard_text() RETURNS text\n"
        "LANGUAGE sql AS $body$\n"
        "\\restrict literal-key\n"
        "\\unrestrict literal-key\n"
        "$body$;\n",
        encoding="utf-8",
    )
    uploaded.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    result = compare_dumps(source, uploaded)
    assert result["status"] == "PASS"
    assert result["pg_dump_guard_keys_normalized"] is False

def test_fingerprint_report_preserves_raw_hash_and_normalizes_only_outer_guard_keys(tmp_path):
    source = tmp_path / "source.sql"
    _write(source, "first-random-key-0123456789")

    result = fingerprint_dump(source)

    assert result["status"] == "PASS"
    assert result["source_prepared_sql_bytes"] == source.stat().st_size
    assert result["source_prepared_sql_sha256"] != result["canonical_sha256"]
    assert result["pg_dump_guard_keys_normalized"] is True
    assert result["only_outer_guard_key_values_normalized"] is True


def test_fingerprint_cli_prints_machine_readable_source_fingerprint(tmp_path):
    source = tmp_path / "source.sql"
    _write(source, "independent-source-key-0123456789")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/compare_cloud_sql_import_dumps.py",
            "--fingerprint",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "PASS"
    assert result["canonical_sha256"] == fingerprint_dump(source)["canonical_sha256"]

