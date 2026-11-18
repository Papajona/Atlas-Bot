from __future__ import annotations

import gzip
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.prepare_cloud_sql_import_dump import prepare_import_dump


def test_plain_sql_is_copied_without_rewriting_or_running_pg_restore(tmp_path):
    source = tmp_path / "source.dump"
    output = tmp_path / "prepared.sql"
    payload = b"CREATE TABLE public.restore_probe(id integer);\n"
    source.write_bytes(payload)

    def unexpected_runner(*_args, **_kwargs):
        raise AssertionError("pg_restore must not run for plain SQL")

    result = prepare_import_dump(source, output, runner=unexpected_runner)

    assert output.read_bytes() == payload
    assert result["input_format"] == "plain_sql_candidate"
    assert result["output_format"] == "plain_sql_candidate"
    assert result["converted"] is False
    assert result["database_connection_attempted"] is False


def test_gzip_sql_is_validated_and_preserved_byte_for_byte(tmp_path):
    source = tmp_path / "source.dump"
    output = tmp_path / "prepared.sql"
    payload = b"CREATE TABLE public.restore_probe(id integer);\n"
    compressed = gzip.compress(payload)
    source.write_bytes(compressed)

    def unexpected_runner(*_args, **_kwargs):
        raise AssertionError("pg_restore must not run for gzip SQL")

    result = prepare_import_dump(source, output, runner=unexpected_runner)

    assert output.read_bytes() == compressed
    assert result["input_format"] == "gzip_sql"
    assert result["output_format"] == "gzip_sql"
    assert result["converted"] is False


def test_custom_archive_is_listed_and_converted_without_database_or_owner_options(tmp_path):
    source = tmp_path / "source.dump"
    output = tmp_path / "prepared.sql"
    source.write_bytes(b"PGDMP\x01\x0farchive")
    sql = b"CREATE TABLE public.restore_probe(id integer);\n"
    calls = []

    def fake_runner(command, **kwargs):
        calls.append(command)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        if command[1] == "--list":
            return SimpleNamespace(returncode=0, stdout="archive TOC", stderr="")
        output_arg = next(arg for arg in command if arg.startswith("--file="))
        Path(output_arg.split("=", 1)[1]).write_bytes(sql)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = prepare_import_dump(source, output, runner=fake_runner)

    assert calls[0][:2] == ["pg_restore", "--list"]
    conversion = calls[1]
    assert conversion[:5] == [
        "pg_restore",
        "--no-owner",
        "--no-privileges",
        "--no-comments",
        "--exit-on-error",
    ]
    assert not any(arg in {"--create", "-C", "--clean", "-c", "--dbname", "-d"} for arg in conversion)
    assert output.read_bytes() == sql
    assert result["input_format"] == "postgresql_custom_archive"
    assert result["output_format"] == "plain_sql"
    assert result["converted"] is True
    assert result["database_connection_attempted"] is False


def test_gzip_wrapped_custom_archive_is_expanded_and_converted(tmp_path):
    source = tmp_path / "source.dump.gz"
    output = tmp_path / "prepared.sql"
    source.write_bytes(gzip.compress(b"PGDMP\x01\x0farchive"))
    calls = []

    def fake_runner(command, **kwargs):
        calls.append(command)
        if command[1] == "--list":
            assert Path(command[2]).read_bytes().startswith(b"PGDMP")
            return SimpleNamespace(returncode=0, stdout="archive TOC", stderr="")
        output_arg = next(arg for arg in command if arg.startswith("--file="))
        Path(output_arg.split("=", 1)[1]).write_text("SELECT 1;\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = prepare_import_dump(source, output, runner=fake_runner)

    assert len(calls) == 2
    assert result["input_format"] == "gzip_postgresql_custom_archive"
    assert result["output_format"] == "plain_sql"
    assert output.read_text(encoding="utf-8") == "SELECT 1;\n"


def test_unreadable_custom_archive_fails_closed_and_leaves_no_output(tmp_path):
    source = tmp_path / "broken.dump"
    output = tmp_path / "prepared.sql"
    source.write_bytes(b"PGDMP\x01\x0fbroken")

    def fake_runner(command, **_kwargs):
        assert command[1] == "--list"
        return SimpleNamespace(returncode=1, stdout="", stderr="invalid archive")

    with pytest.raises(RuntimeError, match="could not read"):
        prepare_import_dump(source, output, runner=fake_runner)
    assert not output.exists()


def test_failed_conversion_removes_partial_sql_output(tmp_path):
    source = tmp_path / "source.dump"
    output = tmp_path / "prepared.sql"
    source.write_bytes(b"PGDMP\x01\x0farchive")

    def fake_runner(command, **_kwargs):
        if command[1] == "--list":
            return SimpleNamespace(returncode=0, stdout="archive TOC", stderr="")
        output.write_text("partial SQL", encoding="utf-8")
        return SimpleNamespace(returncode=1, stdout="", stderr="conversion failed")

    with pytest.raises(RuntimeError, match="could not convert"):
        prepare_import_dump(source, output, runner=fake_runner)
    assert not output.exists()


def test_empty_or_malformed_gzip_backup_fails_closed(tmp_path):
    source = tmp_path / "broken.sql.gz"
    output = tmp_path / "prepared.sql"
    source.write_bytes(b"\x1f\x8bnot-a-valid-gzip")

    with pytest.raises((OSError, EOFError)):
        prepare_import_dump(source, output)
    assert not output.exists()
