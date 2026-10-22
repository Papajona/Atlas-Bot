from __future__ import annotations

from pathlib import Path
import os
import sys
from urllib.parse import quote

import pytest

import scripts.setup_customer_paper_role_interactive as setup_role
from scripts.setup_customer_paper_role_interactive import (
    STAGING_PROJECT_REF,
    _create_url_file_atomically,
    main,
    validate_project_ref,
    POOLER_HOST,
    role_setup_sql,
    validate_runtime_url,
)


PROJECT_REF = "wauaelzyugjuepmrewaw"


def runtime_url(password: str = "safe:/? #secret") -> str:
    return (
        f"postgresql+asyncpg://atlas_customer_paper.{PROJECT_REF}:"
        f"{quote(password, safe='')}@{POOLER_HOST}:5432/postgres?ssl=require"
    )


def test_validates_existing_generated_url_and_decodes_password():
    password = "safe:/? #secret"
    assert validate_runtime_url(runtime_url(password), PROJECT_REF) == password


@pytest.mark.parametrize(
    "uri",
    [
        runtime_url().replace(POOLER_HOST, "wrong.pooler.supabase.com"),
        runtime_url().replace(":5432/", ":6543/"),
        runtime_url().replace("/postgres?", "/other?"),
        runtime_url().replace("ssl=require", "ssl=disable"),
        runtime_url().replace(f"atlas_customer_paper.{PROJECT_REF}", "atlas_customer_paper.other"),
        "postgres://atlas_customer_paper.secret@host:5432/postgres",
    ],
)
def test_rejects_wrong_existing_runtime_url(uri: str):
    with pytest.raises(ValueError):
        validate_runtime_url(uri, PROJECT_REF)


def test_builds_transactional_least_privilege_role_setup_without_printing_secret():
    sql = role_setup_sql("password'with quote")
    assert "BEGIN;" in sql
    assert "CREATE ROLE atlas_customer_paper" in sql
    assert "LOGIN NOINHERIT NOBYPASSRLS" in sql
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION" in sql
    assert "REVOKE atlas_customer_paper FROM CURRENT_USER;" not in sql
    assert "PASSWORD 'password''with quote';" in sql
    assert "supabase_admin" in sql
    assert "to_jsonb(m) ? 'inherit_option'" in sql
    assert "to_jsonb(m) ? 'set_option'" in sql
    assert "unsafe incoming membership" in sql.lower()
    assert sql.rstrip().endswith("SELECT 'PASS: restricted atlas_customer_paper role created and verified';")
    assert "DROP ROLE" not in sql
    assert "BYPASSRLS" in sql
    assert "SUPERUSER" not in sql.replace("NOSUPERUSER", "")


def test_conninfo_uses_native_prompt_and_clears_password_overrides():
    source = Path("scripts/setup_customer_paper_role_interactive.py").read_text(encoding="utf-8")
    assert '"psql", conninfo, "-W", "-X"' in source
    assert '"PGPASSWORD"' in source and '"PGPASSFILE"' in source
    assert "PGSERVICEFILE" in source
    assert "ATLAS_ADMIN_DATABASE_URL" in source



def test_project_ref_validation_rejects_other_valid_ref_before_side_effects(
    tmp_path: Path, monkeypatch
):
    other_valid_ref = "abcdefghijklmnopqrst"
    assert len(other_valid_ref) == 20 and other_valid_ref.isalnum()
    final_path = tmp_path / "not-created" / "database-url"
    monkeypatch.setattr(
        sys,
        "argv",
        ["setup_customer_paper_role_interactive.py", "--project-ref", other_valid_ref,
         "--url-file", str(final_path)],
    )

    def forbidden_connect(*args, **kwargs):
        raise AssertionError("project mismatch must fail before connecting")

    monkeypatch.setattr(setup_role.subprocess, "run", forbidden_connect)
    with pytest.raises(ValueError, match="confirmed existing staging project"):
        main()

    assert not final_path.parent.exists()
    assert not final_path.exists()
    assert validate_project_ref(STAGING_PROJECT_REF) == STAGING_PROJECT_REF


def test_atomic_url_file_write_cleans_up_after_interrupted_write(tmp_path: Path, monkeypatch):
    final_path = tmp_path / "database-url"
    real_fdopen = os.fdopen

    class InterruptedWriter:
        def __init__(self, fd, mode, encoding=None):
            self.stream = real_fdopen(fd, mode, encoding=encoding)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.stream.close()
            return False

        def write(self, value):
            self.stream.write(value[:7])
            self.stream.flush()
            raise OSError("simulated interrupted URL-file write")

        def flush(self):
            self.stream.flush()

        def fileno(self):
            return self.stream.fileno()

    monkeypatch.setattr(
        setup_role.os,
        "fdopen",
        lambda fd, mode, encoding=None: InterruptedWriter(fd, mode, encoding),
    )

    with pytest.raises(OSError, match="simulated interrupted"):
        _create_url_file_atomically(final_path, runtime_url())

    assert not final_path.exists()
    assert list(tmp_path.glob(".database-url.*.tmp")) == []


def test_atomic_url_file_publication_never_overwrites_existing_file(tmp_path: Path):
    final_path = tmp_path / "database-url"
    original = runtime_url("original-secret")
    final_path.write_text(original + "\n", encoding="utf-8")
    final_path.chmod(0o600)

    with pytest.raises(FileExistsError):
        _create_url_file_atomically(final_path, runtime_url("replacement-secret"))

    assert final_path.read_text(encoding="utf-8") == original + "\n"
    assert list(tmp_path.glob(".database-url.*.tmp")) == []
