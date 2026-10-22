#!/usr/bin/env python3
"""Explicit, guarded customer-paper schema provisioning for the existing Supabase project.

This is an operator-run DDL tool. It is never called by the application or CI.
It does not create a Supabase project, paid branch, database role password, or backup.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit


PROJECT_REF_RE = re.compile(r"^[a-z0-9]{20}$")
SESSION_POOLER_HOST_RE = re.compile(r"^aws-\d+-[a-z0-9]+(?:-[a-z0-9]+)*\.pooler\.supabase\.com$")
RUNTIME_ROLE = "atlas_customer_paper"
SCHEMA_FILE = Path(__file__).resolve().with_name("customer_paper_ledger_schema.sql")

IDENTITY_SQL = """
SELECT current_database() || '|' || current_user || '|' ||
       current_setting('transaction_read_only') || '|' ||
       has_database_privilege(current_user, current_database(), 'CREATE')::text || '|' ||
       (NOT EXISTS (
          SELECT 1 FROM pg_namespace n
          WHERE n.nspname='customer_paper'
            AND pg_get_userbyid(n.nspowner) <> current_user
       ))::text || '|' ||
       EXISTS (
          SELECT 1 FROM pg_roles r
          WHERE r.rolname='atlas_customer_paper'
            AND r.rolcanlogin AND NOT r.rolsuper AND NOT r.rolinherit
            AND NOT r.rolcreatedb AND NOT r.rolcreaterole
            AND NOT r.rolreplication AND NOT r.rolbypassrls
            AND NOT EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid)
            AND NOT EXISTS (
              SELECT 1
              FROM pg_auth_members m
              JOIN pg_roles member_role ON member_role.oid=m.member
              WHERE m.roleid=r.oid
                AND NOT (
                  member_role.rolname='postgres'
                  AND m.admin_option
                  AND COALESCE((to_jsonb(m)->>'inherit_option')::boolean, false)=false
                  AND COALESCE((to_jsonb(m)->>'set_option')::boolean, false)=false
                )
            )
            AND (SELECT count(*) FROM pg_auth_members m WHERE m.roleid=r.oid) <= 1
       )::text || '|' ||
       (SELECT (rolsuper OR rolcreaterole)::text FROM pg_roles WHERE rolname=current_user);
"""

ROLE_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='atlas_customer_paper')::text;"


VERIFY_SQL = """
SELECT
  (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='customer_paper' AND c.relkind IN ('r','p')) || '|' ||
  (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='customer_paper' AND c.relkind IN ('r','p')
     AND c.relname IN ('accounts','journals','journal_lines','orders','fills','order_events',
                       'position_events','market_bar_budgets','market_bar_allocations')) || '|' ||
  (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='customer_paper' AND c.relkind IN ('r','p')
     AND c.relname IN ('accounts','journals','journal_lines','orders','fills','order_events','position_events')
     AND c.relrowsecurity AND c.relforcerowsecurity) || '|' ||
  (SELECT count(*) FROM pg_policies WHERE schemaname='customer_paper') || '|' ||
  (SELECT count(*) FROM pg_policies
   WHERE schemaname='customer_paper' AND policyname='customer_paper_tenant'
     AND tablename IN ('accounts','journals','journal_lines','orders','fills','order_events','position_events')) || '|' ||
  (SELECT count(*) FROM pg_policies
   WHERE schemaname='customer_paper' AND policyname='customer_paper_bar_budget_backend'
     AND tablename IN ('market_bar_budgets','market_bar_allocations')) || '|' ||
  has_schema_privilege('atlas_customer_paper','customer_paper','USAGE')::text || '|' ||
  (NOT has_schema_privilege('atlas_customer_paper','customer_paper','CREATE'))::text || '|' ||
  (NOT has_schema_privilege('atlas_customer_paper','public','CREATE'))::text || '|' ||
  (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='customer_paper' AND c.relkind='S') || '|' ||
  (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='customer_paper' AND c.relkind='S'
     AND has_sequence_privilege('atlas_customer_paper',c.oid,'USAGE')) || '|' ||
  (NOT EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public'
      AND c.relname IN ('customer_ledger_accounts','ledger_journals','ledger_journal_lines',
                        'ledger_entries','withdrawals','wallets','funding_transactions','tron_sweeps')
      AND (has_table_privilege('atlas_customer_paper',c.oid,'SELECT')
        OR has_table_privilege('atlas_customer_paper',c.oid,'INSERT')
        OR has_table_privilege('atlas_customer_paper',c.oid,'UPDATE')
        OR has_table_privilege('atlas_customer_paper',c.oid,'DELETE')
        OR has_table_privilege('atlas_customer_paper',c.oid,'TRUNCATE')
        OR has_table_privilege('atlas_customer_paper',c.oid,'REFERENCES')
        OR has_table_privilege('atlas_customer_paper',c.oid,'TRIGGER'))
  ))::text;
"""


def _pgpass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _runtime_connection_uri(
    expected_ref: str, password: str, database_host: str | None = None
) -> str:
    """Build a TLS-required runtime URL for the same verified direct or session-pooler endpoint."""
    if not PROJECT_REF_RE.fullmatch(expected_ref):
        raise ValueError("Expected an explicit 20-character Supabase project ref")
    if not password:
        raise ValueError("Runtime database password must not be empty")
    host = (database_host or f"db.{expected_ref}.supabase.co").lower()
    if host == f"db.{expected_ref}.supabase.co":
        username_value = RUNTIME_ROLE
    elif SESSION_POOLER_HOST_RE.fullmatch(host):
        # Supavisor requires the project reference suffix in pooler usernames.
        username_value = f"{RUNTIME_ROLE}.{expected_ref}"
    else:
        raise ValueError("Runtime DB host must be the configured project's direct endpoint or a Supabase session pooler host")
    username = quote(username_value, safe="")
    encoded_password = quote(password, safe="")
    return urlunsplit((
        "postgresql+asyncpg",
        f"{username}:{encoded_password}@{host}:5432",
        "/postgres",
        "ssl=require",
        "",
    ))


def _connection_env(database_url: str, expected_ref: str, passfile: Path) -> dict[str, str]:
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql", "postgresql+asyncpg", "postgresql+psycopg"}:
        raise ValueError("ATLAS_ADMIN_DATABASE_URL must be a PostgreSQL URI")
    host = (parsed.hostname or "").lower()
    direct_host = f"db.{expected_ref}.supabase.co"
    is_pooler = bool(SESSION_POOLER_HOST_RE.fullmatch(host))
    if host != direct_host and not is_pooler:
        raise ValueError("Admin database host must be the configured project's direct endpoint or a Supabase session pooler host")
    if (parsed.path or "").lstrip("/") != "postgres":
        raise ValueError("Admin database URL must target the existing postgres database")
    if not parsed.username or not parsed.password:
        raise ValueError("Admin database URL must include username and password")
    if parsed.port not in (None, 5432):
        raise ValueError("Provisioning requires port 5432 (the Supabase session pooler, not transaction pooler)")
    if is_pooler and unquote(parsed.username) != f"postgres.{expected_ref}":
        raise ValueError("Session-pooler admin username must be postgres.<configured-project-ref>")
    password = unquote(parsed.password)
    fields = [
        _pgpass_escape(parsed.hostname),
        _pgpass_escape(str(parsed.port or 5432)),
        _pgpass_escape("postgres"),
        _pgpass_escape(unquote(parsed.username)),
        _pgpass_escape(password),
    ]
    passfile.write_text(":".join(fields) + "\n", encoding="utf-8")
    passfile.chmod(0o600)
    env = os.environ.copy()
    # Never pass the admin URI/password in psql argv or the child process environment.
    env.pop("ATLAS_ADMIN_DATABASE_URL", None)
    env.pop("DATABASE_URL", None)
    env.pop("PGPASSWORD", None)
    env.update({
        "PGHOST": parsed.hostname,
        "PGPORT": str(parsed.port or 5432),
        "PGDATABASE": "postgres",
        "PGUSER": unquote(parsed.username),
        "PGPASSFILE": str(passfile),
        "PGSSLMODE": "require",
        "PGOPTIONS": "-c statement_timeout=15000",
    })
    for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"):
        env.pop(key, None)
    return env


def _role_setup_sql(password: str) -> str:
    """Create the restricted role and remove PostgreSQL 16+ creator membership."""
    escaped_password = password.replace("'", "''")
    return (
        "BEGIN;\n"
        "CREATE ROLE atlas_customer_paper LOGIN NOINHERIT NOBYPASSRLS "
        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;\n"
        # PostgreSQL 16+ grants a non-superuser CREATEROLE creator membership
        # with ADMIN OPTION. Remove it inside the transaction so the runtime
        # role satisfies the no-memberships invariant before read-back checks.
        "REVOKE atlas_customer_paper FROM CURRENT_USER;\n"
        f"ALTER ROLE atlas_customer_paper PASSWORD '{escaped_password}';\n"
        "GRANT CONNECT ON DATABASE postgres TO atlas_customer_paper;\n"
        "COMMIT;\n"
    )


def _psql(env: dict[str, str], *args: str) -> str:
    result = subprocess.run(
        ["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        # Do not print the connection URI or any environment values.
        message = result.stderr.strip().splitlines()
        raise RuntimeError("psql failed: " + (message[-1] if message else "database operation failed"))
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--setup-role", action="store_true",
        help="Create the least-privilege runtime role and write its connection URI to a mode-0600 local file",
    )
    mode.add_argument(
        "--apply", action="store_true",
        help="Explicitly apply the reviewed, transactional customer-paper schema DDL",
    )
    args = parser.parse_args()

    admin_url = os.environ.get("ATLAS_ADMIN_DATABASE_URL", "")
    staging_ref = os.environ.get("ATLAS_STAGING_SUPABASE_PROJECT_REF", "")
    production_ref = os.environ.get("ATLAS_PRODUCTION_SUPABASE_PROJECT_REF", "")
    backup_ref = os.environ.get("ATLAS_CUSTOMER_PAPER_BACKUP_EVIDENCE_URI", "").strip()

    if not admin_url:
        raise ValueError("ATLAS_ADMIN_DATABASE_URL is required (keep it in a protected local environment)")
    if not PROJECT_REF_RE.fullmatch(staging_ref) or not PROJECT_REF_RE.fullmatch(production_ref):
        raise ValueError("Both explicit 20-character Supabase project refs are required")
    if staging_ref != production_ref:
        raise ValueError("This operator flow is for the approved existing single Supabase project only")
    if args.apply:
        if not backup_ref:
            raise ValueError("Set ATLAS_CUSTOMER_PAPER_BACKUP_EVIDENCE_URI after independently checking a recoverable backup/export")
        backup = urlsplit(backup_ref)
        if backup.scheme not in {"https", "gs"} or not backup.netloc:
            raise ValueError("Backup evidence reference must be an https:// or gs:// URI")
        if not SCHEMA_FILE.is_file():
            raise ValueError("Reviewed scripts/customer_paper_ledger_schema.sql is missing")

    with tempfile.TemporaryDirectory(prefix="atlas-paper-provision-") as temporary:
        passfile = Path(temporary) / "pgpass"
        env = _connection_env(admin_url, staging_ref, passfile)
        identity = _psql(env, "-c", IDENTITY_SQL).split("|")
        if len(identity) != 7:
            raise RuntimeError("Could not verify connected PostgreSQL identity")
        database, role, read_only, can_create, schema_owner_ok, paper_role_ok, can_manage_roles = identity
        if database != "postgres":
            raise RuntimeError("Refusing operation: connected database is not postgres")
        if read_only.strip().lower() not in {"off", "false", "f"}:
            raise RuntimeError("Refusing operation: database connection is read-only")
        if schema_owner_ok.strip().lower() not in {"true", "t"}:
            raise RuntimeError("Refusing operation: an existing customer_paper schema has a different owner")
        if role in {"atlas_customer_paper", "anon", "authenticated", "service_role", "supabase_admin", "supabase_auth_admin", "authenticator"}:
            raise RuntimeError("Refusing operation: current connection role is not an appropriate reviewed owner")
        print("Target verified: existing Supabase project ref, database=postgres.")
        print(f"Current database role: {role}. Paper runtime role: {RUNTIME_ROLE}.")
        confirmation = input(
            "Type the exact existing Supabase project ref to continue this operator operation: "
        ).strip()
        if confirmation != staging_ref:
            raise RuntimeError("Confirmation did not match the configured existing Supabase project ref")

        if args.setup_role:
            role_exists = _psql(env, "-c", ROLE_EXISTS_SQL).strip().lower() in {"t", "true"}
            if role_exists:
                raise RuntimeError("atlas_customer_paper already exists; inspect its attributes/ACLs instead of replacing its password")
            if can_manage_roles.strip().lower() not in {"true", "t"}:
                raise RuntimeError("Refusing role creation: current database role lacks CREATEROLE/SUPERUSER authority")
            repo_root = Path(__file__).resolve().parents[1]
            output_path = Path.home().resolve() / ".atlas-customer-paper" / "database-url"
            try:
                output_path.relative_to(repo_root)
            except ValueError:
                pass
            else:
                raise RuntimeError("Refusing to write the paper database URL inside the repository")
            if output_path.exists() or output_path.is_symlink():
                raise RuntimeError("Refusing to overwrite an existing customer paper URL file")
            output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if output_path.parent.stat().st_mode & 0o077:
                raise RuntimeError("Credential output directory must be private (permissions 0700 or stricter)")
            password = secrets.token_urlsafe(48)
            connection_uri = _runtime_connection_uri(staging_ref, password, urlsplit(admin_url).hostname)
            # Keep a protected copy before attempting role DDL so an ambiguous connection
            # failure cannot leave an orphan role with a lost generated password.
            output_path.write_text(connection_uri + "\n", encoding="utf-8")
            output_path.chmod(0o600)
            role_sql = _role_setup_sql(password)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix="atlas-paper-role-", suffix=".sql",
                dir=temporary, delete=False,
            ) as role_file:
                role_file.write(role_sql)
                role_file.flush()
                os.fchmod(role_file.fileno(), 0o600)
                role_file_path = Path(role_file.name)
            try:
                _psql(env, "-f", str(role_file_path))
            finally:
                role_file_path.unlink(missing_ok=True)
            verify_role = _psql(env, "-c", IDENTITY_SQL).split("|")
            if len(verify_role) != 7 or verify_role[5].strip().lower() not in {"true", "t"}:
                raise RuntimeError("Role creation may have committed but read-back verification failed; preserve the protected URL file, inspect manually, and keep the feature disabled")
            print("PASS: least-privilege atlas_customer_paper login created and verified.")
            print("A mode-0600 connection URI file was written; the generated password was not printed.")
            print(f"Securely transfer this file into the approved secret store as CUSTOMER_PAPER_DATABASE_URL: {output_path}")
            print("Delete the local URL file after confirming the secret version was stored; do not commit or paste its contents.")
            return 0

        if not backup_ref:
            raise ValueError("Set ATLAS_CUSTOMER_PAPER_BACKUP_EVIDENCE_URI after independently checking a recoverable backup/export")
        if can_create.strip().lower() not in {"true", "t"}:
            raise RuntimeError("Refusing DDL: current role lacks CREATE on the existing database")
        if paper_role_ok.strip().lower() not in {"true", "t"}:
            raise RuntimeError("Refusing DDL: atlas_customer_paper is missing or its role attributes/memberships are unsafe")
        print("Backup evidence reference was supplied; this script does not independently verify the referenced backup.")
        _psql(
            env,
            "-v", f"paper_runtime_role={RUNTIME_ROLE}",
            "-1",
            "-f", str(SCHEMA_FILE),
        )
        verification = _psql(env, "-c", VERIFY_SQL).split("|")
        if len(verification) != 12:
            raise RuntimeError("Schema DDL ran, but post-DDL verification output was incomplete; keep feature disabled")
        (table_count, expected_table_count, forced_rls_count, policy_count, tenant_policy_count,
         backend_policy_count, schema_usage, schema_create_denied, public_create_denied,
         sequence_count, sequence_usage_count, real_tables_denied) = verification
        checks = {
            "nine_paper_tables_only": table_count == "9" and expected_table_count == "9",
            "forced_rls_on_seven_customer_tables": forced_rls_count == "7",
            "nine_expected_policies": policy_count == "9",
            "tenant_policy_on_seven_customer_tables": tenant_policy_count == "7",
            "backend_policy_on_two_capacity_tables": backend_policy_count == "2",
            "paper_schema_usage": schema_usage.strip().lower() in {"true", "t"},
            "paper_schema_create_denied": schema_create_denied.strip().lower() in {"true", "t"},
            "public_schema_create_denied": public_create_denied.strip().lower() in {"true", "t"},
            "four_identity_sequences_only": sequence_count == "4",
            "all_identity_sequences_have_usage": sequence_usage_count == "4",
            "real_money_table_privileges_denied": real_tables_denied.strip().lower() in {"true", "t"},
        }
        for name, passed in checks.items():
            print(f"{'PASS' if passed else 'FAIL'} {name}")
        print("Next: store the separate runtime credential in the approved secret store, run the read-only application preflight, and leave CUSTOMER_PAPER_LEDGER_ENABLED=false until it reports PASS.")
        print("This script does not run customer trades, certify restore/reconciliation, or enable live money movement.")
        return 0 if all(checks.values()) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2)
