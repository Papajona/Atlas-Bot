from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from scripts.provision_customer_paper_schema import (
    VERIFY_SQL,
    _connection_env,
    _pgpass_escape,
    _role_setup_sql,
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




def test_admin_connection_accepts_project_bound_session_pooler_uri(tmp_path: Path):
    passfile = tmp_path / "pgpass-pooler"
    uri = (
        f"postgresql://postgres.{PROJECT_REF}:pass%3Aword@"
        "aws-0-eu-west-1.pooler.supabase.com:5432/postgres"
    )
    env = _connection_env(uri, PROJECT_REF, passfile)
    assert env["PGHOST"] == "aws-0-eu-west-1.pooler.supabase.com"
    assert env["PGPORT"] == "5432"
    assert env["PGUSER"] == f"postgres.{PROJECT_REF}"
    assert passfile.read_text(encoding="utf-8").startswith(
        f"aws-0-eu-west-1.pooler.supabase.com:5432:postgres:postgres.{PROJECT_REF}:"
    )


@pytest.mark.parametrize(
    "uri",
    [
        f"postgresql://postgres.wrongproject:secret@aws-0-eu-west-1.pooler.supabase.com:5432/postgres",
        f"postgresql://postgres.{PROJECT_REF}:secret@aws-0-eu-west-1.pooler.supabase.com:6543/postgres",
        f"postgresql://postgres.{PROJECT_REF}:secret@not-a-pooler.supabase.com:5432/postgres",
    ],
)
def test_admin_connection_rejects_wrong_session_pooler_target(uri: str, tmp_path: Path):
    with pytest.raises(ValueError):
        _connection_env(uri, PROJECT_REF, tmp_path / "pgpass-invalid")


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


def test_role_setup_revokes_postgres_16_creator_membership_before_readback():
    sql = _role_setup_sql("safe'password")

    assert sql.index("CREATE ROLE atlas_customer_paper") < sql.index(
        "REVOKE atlas_customer_paper FROM CURRENT_USER;"
    ) < sql.index("ALTER ROLE atlas_customer_paper PASSWORD")
    assert "PASSWORD 'safe''password';" in sql
    assert sql.rstrip().endswith("COMMIT;")


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



def test_runtime_uri_uses_session_pooler_username_suffix():
    host = "aws-0-eu-west-1.pooler.supabase.com"
    uri = _runtime_connection_uri(PROJECT_REF, "another secret", host)
    parsed = urlsplit(uri)
    assert parsed.hostname == host
    assert parsed.port == 5432
    assert unquote(parsed.username or "") == f"atlas_customer_paper.{PROJECT_REF}"
    assert parse_qs(parsed.query) == {"ssl": ["require"]}


def test_runtime_uri_rejects_unverified_host():
    with pytest.raises(ValueError):
        _runtime_connection_uri(PROJECT_REF, "secret", "pooler.invalid.example")


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
