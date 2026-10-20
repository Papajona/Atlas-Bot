from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from scripts.provision_customer_paper_schema import (
    VERIFY_SQL,
    _connection_env,
    _pgpass_escape,
    _runtime_connection_uri,
)


PROJECT_REF = "abcdefghijklmnopqrst"


def test_admin_connection_uses_private_pgpass_not_argv_or_environment(tmp_path: Path):
    passfile = tmp_path / "pgpass"
    uri = (
        f"postgresql://atlas_admin:password%3Awith%5C%5Cbackslash@"
        f"db.{PROJECT_REF}.supabase.co:5432/postgres"
    )
    env = _connection_env(uri, PROJECT_REF, passfile)

    assert env["PGHOST"] == f"db.{PROJECT_REF}.supabase.co"
    assert env["PGDATABASE"] == "postgres"
    assert env["PGUSER"] == "atlas_admin"
    assert env["PGSSLMODE"] == "require"
    assert "ATLAS_ADMIN_DATABASE_URL" not in env
    assert "DATABASE_URL" not in env
    assert "PGPASSWORD" not in env
    assert passfile.stat().st_mode & 0o777 == 0o600
    assert passfile.read_text(encoding="utf-8").startswith(
        f"db.{PROJECT_REF}.supabase.co:5432:postgres:atlas_admin:"
    )
    assert _pgpass_escape("colon:backslash\\") == "colon\\:backslash\\\\"


@pytest.mark.parametrize(
    "uri",
    [
        "postgresql://atlas_admin:secret@other-project.supabase.co:5432/postgres",
        f"postgresql://atlas_admin:secret@db.{PROJECT_REF}.supabase.co:6543/postgres",
        f"postgresql://atlas_admin:secret@db.{PROJECT_REF}.supabase.co:5432/other",
        f"https://atlas_admin:secret@db.{PROJECT_REF}.supabase.co:5432/postgres",
    ],
)
def test_admin_connection_rejects_wrong_host_port_database_or_scheme(uri: str, tmp_path: Path):
    with pytest.raises(ValueError):
        _connection_env(uri, PROJECT_REF, tmp_path / "pgpass")


def test_schema_apply_is_single_transaction_and_resets_old_acl_grants():
    sql = Path("scripts/customer_paper_ledger_schema.sql").read_text(encoding="utf-8")
    provisioner = Path("scripts/provision_customer_paper_schema.py").read_text(encoding="utf-8")
    assert "\\set ON_ERROR_STOP on" in sql
    assert "\\if :{?paper_runtime_role}" in sql
    assert (
        '_psql(\n            env,\n            "-v", f"paper_runtime_role={RUNTIME_ROLE}",\n'
        '            "-1",\n            "-f", str(SCHEMA_FILE),\n        )'
    ) in provisioner
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA customer_paper FROM %I" in sql
    assert "REVOKE ALL ON ALL SEQUENCES IN SCHEMA customer_paper FROM %I" in sql
    assert "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA customer_paper FROM %I" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY customer_paper_tenant" in sql


def test_runtime_customer_paper_uri_requires_tls_and_encodes_credentials():
    password = "dummy:/? #credential"
    uri = _runtime_connection_uri(PROJECT_REF, password)
    parsed = urlsplit(uri)
    assert parsed.scheme == "postgresql+asyncpg"
    assert parsed.hostname == f"db.{PROJECT_REF}.supabase.co"
    assert parsed.port == 5432
    assert unquote(parsed.username or "") == "atlas_customer_paper"
    assert unquote(parsed.password or "") == password
    assert parse_qs(parsed.query) == {"ssl": ["require"]}


def test_runtime_customer_paper_uri_rejects_invalid_project_ref_or_empty_password():
    with pytest.raises(ValueError):
        _runtime_connection_uri("wrong-project-ref", "dummy")
    with pytest.raises(ValueError):
        _runtime_connection_uri(PROJECT_REF, "")


def test_provision_verifier_matches_all_nine_current_schema_tables_and_policy_types():
    schema = Path("scripts/customer_paper_ledger_schema.sql").read_text(encoding="utf-8")
    expected_tables = {
        "accounts", "journals", "journal_lines", "orders", "fills", "order_events",
        "position_events", "market_bar_budgets", "market_bar_allocations",
    }
    actual_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS customer_paper\.(\w+)", schema))
    assert actual_tables == expected_tables
    for table in ("accounts", "journals", "journal_lines", "orders", "fills", "order_events", "position_events"):
        assert f"ALTER TABLE customer_paper.{table} FORCE ROW LEVEL SECURITY" in schema
    assert "CREATE POLICY customer_paper_tenant" in schema
    assert "CREATE POLICY customer_paper_bar_budget_backend" in schema

    assert "'market_bar_budgets','market_bar_allocations'" in VERIFY_SQL
    assert "policyname='customer_paper_tenant'" in VERIFY_SQL
    assert "policyname='customer_paper_bar_budget_backend'" in VERIFY_SQL
    source = Path("scripts/provision_customer_paper_schema.py").read_text(encoding="utf-8")
    assert "len(verification) != 12" in source
    assert '"nine_paper_tables_only": table_count == "9" and expected_table_count == "9"' in source
