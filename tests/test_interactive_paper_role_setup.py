from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest

from scripts.setup_customer_paper_role_interactive import (
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
    assert "REVOKE atlas_customer_paper FROM CURRENT_USER;" in sql
    assert "PASSWORD 'password''with quote';" in sql
    assert "unexpected role memberships" in sql.lower()
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
