from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest

from scripts.provision_customer_paper_schema import (
    IDENTITY_SQL,
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
        "postgresql://postgres.wrongproject:secret@aws-0-eu-west-1.pooler.supabase.com:5432/postgres",
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


def test_role_setup_accepts_only_supabase_managed_control_plane_membership():
    sql = _role_setup_sql("safe'password")

    assert sql.index("CREATE ROLE atlas_customer_paper") < sql.index(
        "ALTER ROLE atlas_customer_paper PASSWORD"
    ) < sql.index("GRANT CONNECT ON DATABASE postgres TO atlas_customer_paper")
    assert "REVOKE atlas_customer_paper FROM CURRENT_USER;" not in sql
    assert "PASSWORD 'safe''password';" in sql
    assert "member_role.rolname='postgres'" in IDENTITY_SQL
    assert "grantor_role.rolname='supabase_admin'" in IDENTITY_SQL
    assert "to_jsonb(m) ? 'inherit_option'" in IDENTITY_SQL
    assert "to_jsonb(m) ? 'set_option'" in IDENTITY_SQL
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


def test_gcp_bootstrap_reuses_provisioner_generated_paper_url_without_retyping_password():
    script = Path("scripts/bootstrap_atlas_paper_staging_gcp.sh").read_text(encoding="utf-8")
    assert 'PAPER_URL_FILE="${HOME}/.atlas-customer-paper/database-url"' in script
    assert 'put_secret_file "$PAPER_SECRET" "$PAPER_URL_FILE"' in script
    assert 'atlas_customer_paper.${SUPABASE_PROJECT_REF}' in script
    assert 'db.${SUPABASE_PROJECT_REF}.supabase.co' not in script


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



def _extract_shell_function(script: str, name: str) -> str:
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}\n", script)
    assert match is not None, f"{name}() must be present for behavioral testing"
    return match.group(0)


def _run_paper_bootstrap_subset(
    tmp_path: Path,
    file_bytes: bytes,
    *,
    fail_paper_add: bool = False,
    primary_password: str = "primary-not-real",
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    script = Path("scripts/bootstrap_atlas_paper_staging_gcp.sh").read_text(encoding="utf-8")
    validator = _extract_shell_function(script, "validate_paper_url_file")
    put_secret = _extract_shell_function(script, "put_secret")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gcloud_log = tmp_path / "gcloud.log"
    payload_dir = tmp_path / "payloads"
    payload_dir.mkdir()
    gcloud_stub = fake_bin / "gcloud"
    gcloud_stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%s\\n' \"$*\" >> \"$GCLOUD_LOG\"\n"
        "if [[ \"$1\" == \"secrets\" && \"$2\" == \"versions\" && \"$3\" == \"add\" ]]; then\n"
        "  cat > \"$SECRET_PAYLOAD_DIR/$4\"\n"
        "  if [[ \"$4\" == \"$PAPER_SECRET\" && \"$GCLOUD_FAIL_PAPER_ADD\" == \"1\" ]]; then exit 9; fi\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    os.chmod(gcloud_stub, 0o755)

    url_file = tmp_path / "database-url"
    url_file.write_bytes(file_bytes)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
            "GCLOUD_LOG": str(gcloud_log),
            "SECRET_PAYLOAD_DIR": str(payload_dir),
            "PROJECT_ID": "atlas-staging-510915",
            "GCLOUD_FAIL_PAPER_ADD": "1" if fail_paper_add else "0",
            "SUPABASE_PROJECT_REF": "abcdefghijklmnopqrst",
            "SUPABASE_DB_HOST": "aws-0-eu-west-1.pooler.supabase.com",
            "PAPER_URL_FILE": str(url_file),
            "PRIMARY_SECRET": "atlas-staging-primary-db-url",
            "PAPER_SECRET": "atlas-staging-customer-paper-db-url",
            "PRIMARY_PASSWORD": primary_password,
            "PRIMARY_URL": "postgresql+asyncpg://atlas_paper_reader.abcdefghijklmnopqrst:primary-not-real@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?ssl=require",
        }
    )
    # This is the production order: validate/capture first, then mutate either DB secret.
    command = (
        "set -euo pipefail\n"
        + validator
        + "\n"
        + put_secret
        + "\n"
        + 'if ! PAPER_URL="$(validate_paper_url_file "$PAPER_URL_FILE")"; then exit 17; fi\n'
        + 'if [[ -z "$PAPER_URL" ]]; then exit 18; fi\n'
        + 'put_secret "$PRIMARY_SECRET" "$PRIMARY_URL"\n'
        + 'put_secret "$PAPER_SECRET" "$PAPER_URL"\n'
    )
    completed = subprocess.run(
        ["bash", "-c", command],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed, gcloud_log, payload_dir


@pytest.mark.parametrize(
    "file_bytes",
    [
        b"",
        b"postgresql://user:pass@host/db\nextra\n",
        b"postgresql://user:pass@host/db\r\n",
        b"postgresql://user:pass@host/db\x00",
        b"\xff",
        b"not-a-database-url",
        b"postgresql://atlas_customer_paper.abcdefghijklmnopqrst:secret@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?ssl=require",
        b"postgresql+asyncpg://atlas_customer_paper.wrongproject:secret@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?ssl=require",
        b"postgresql+asyncpg://atlas_customer_paper.abcdefghijklmnopqrst:secret@wronghost.example:5432/postgres?ssl=require",
        b"postgresql+asyncpg://atlas_customer_paper.abcdefghijklmnopqrst:secret@aws-0-eu-west-1.pooler.supabase.com:6543/postgres?ssl=require",
        b"postgresql+asyncpg://atlas_customer_paper.abcdefghijklmnopqrst:secret@aws-0-eu-west-1.pooler.supabase.com:5432/other?ssl=require",
        b"postgresql+asyncpg://atlas_customer_paper.abcdefghijklmnopqrst:secret@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?ssl=disable",
        b"postgresql+asyncpg://atlas_customer_paper.abcdefghijklmnopqrst:bad%ZZescape@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?ssl=require",
    ],
)
def test_gcp_paper_url_validation_fails_before_any_secret_manager_call(
    tmp_path: Path, file_bytes: bytes
):
    completed, gcloud_log, _ = _run_paper_bootstrap_subset(tmp_path, file_bytes)

    assert completed.returncode != 0
    assert not gcloud_log.exists() or gcloud_log.read_bytes() == b""


def test_gcp_paper_url_is_validated_before_primary_secret_is_touched(tmp_path: Path):
    script = Path("scripts/bootstrap_atlas_paper_staging_gcp.sh").read_text(encoding="utf-8")
    validate_call = script.index('PAPER_URL="$(validate_paper_url_file "$PAPER_URL_FILE")"')
    primary_secret_write = script.index('put_secret "$PRIMARY_SECRET" "$PRIMARY_URL"')
    paper_secret_write = script.index('put_secret "$PAPER_SECRET" "$PAPER_URL"')

    assert validate_call < primary_secret_write < paper_secret_write
    assert "put_secret_file" not in script


def test_gcp_paper_url_upload_is_exact_and_keeps_uri_out_of_gcloud_argv(tmp_path: Path):
    uri = (
        "postgresql+asyncpg://atlas_customer_paper.abcdefghijklmnopqrst:URI_SENTINEL_ONLY_ON_STDIN"
        "@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?ssl=require"
    )
    completed, gcloud_log, payload_dir = _run_paper_bootstrap_subset(
        tmp_path, (uri + "\n").encode("utf-8")
    )

    assert completed.returncode == 0, completed.stderr
    assert (payload_dir / "atlas-staging-customer-paper-db-url").read_bytes() == uri.encode("utf-8")
    logged_args = gcloud_log.read_text(encoding="utf-8")
    assert "URI_SENTINEL_ONLY_ON_STDIN" not in logged_args
    assert "secrets describe atlas-staging-primary-db-url" in logged_args
    assert "secrets versions add atlas-staging-primary-db-url" in logged_args
    assert "secrets describe atlas-staging-customer-paper-db-url" in logged_args
    assert "secrets versions add atlas-staging-customer-paper-db-url" in logged_args


def test_gcp_paper_secret_upload_failure_returns_nonzero(tmp_path: Path):
    uri = (
        "postgresql+asyncpg://atlas_customer_paper.abcdefghijklmnopqrst:dummy"
        "@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?ssl=require"
    )
    completed, _, _ = _run_paper_bootstrap_subset(
        tmp_path, uri.encode("utf-8"), fail_paper_add=True
    )

    assert completed.returncode != 0




def test_file_backed_bootstrap_rejects_matching_primary_password_before_secret_writes(
    tmp_path: Path,
):
    secret = "same-password-not-for-logs"
    uri = (
        "postgresql+asyncpg://atlas_customer_paper.abcdefghijklmnopqrst:"
        f"{quote(secret, safe='')}@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?ssl=require"
    )
    result, gcloud_log, _ = _run_paper_bootstrap_subset(
        tmp_path, uri.encode("utf-8"), primary_password=secret
    )

    assert result.returncode != 0
    assert not gcloud_log.exists() or gcloud_log.read_bytes() == b""
    assert "different passwords" in result.stderr
    assert secret not in result.stdout + result.stderr


def test_file_backed_bootstrap_accepts_distinct_passwords_without_disclosing_them(
    tmp_path: Path,
):
    paper_password = "distinct-paper-secret"
    primary_password = "different-primary-secret"
    uri = (
        "postgresql+asyncpg://atlas_customer_paper.abcdefghijklmnopqrst:"
        f"{quote(paper_password, safe='')}@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?ssl=require"
    )
    result, gcloud_log, payload_dir = _run_paper_bootstrap_subset(
        tmp_path, (uri + "\n").encode("utf-8"), primary_password=primary_password
    )

    assert result.returncode == 0, result.stderr
    assert (payload_dir / "atlas-staging-primary-db-url").exists()
    assert (payload_dir / "atlas-staging-customer-paper-db-url").read_bytes() == uri.encode("utf-8")
    assert "distinct credential" not in result.stdout + result.stderr
    assert paper_password not in result.stdout + result.stderr
    assert primary_password not in result.stdout + result.stderr
