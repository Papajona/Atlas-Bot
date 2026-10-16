#!/usr/bin/env python3
"""Read-only integrity audit for a database restored from an identified backup.

The operator must restore the selected backup into a distinct, isolated staging target
before running this script. This program will not create/restore/migrate/drop data.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse


QUERY = r"""
SELECT jsonb_build_object(
  'database', current_database(),
  'current_user', current_user,
  'session_user', session_user,
  'revision', (SELECT string_agg(version_num, ',' ORDER BY version_num) FROM public.alembic_version),
  'public_table_count', (
    SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relkind IN ('r','p')
  ),
  -- The Alembic marker alone is insufficient: a partial restore or manual version
  -- edit must not pass while core paper lifecycle/research objects are absent.
  'required_tables_missing', (
    SELECT coalesce(jsonb_agg(required.table_name ORDER BY required.table_name), '[]'::jsonb)
    FROM unnest(ARRAY[
      'paper_order_states', 'paper_bar_states', 'paper_fill_events',
      'research_trial_archive'
    ]::text[]) AS required(table_name)
    WHERE to_regclass(format('public.%I', required.table_name)) IS NULL
  ),
  'tables_without_rls', (
    SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relkind IN ('r','p') AND NOT c.relrowsecurity
  ),
  'negative_customer_ledger_buckets', (
    SELECT count(*) FROM public.customer_ledger_accounts
    WHERE available < 0 OR trading_reserved < 0 OR withdrawal_reserved < 0
  ),
  'unbalanced_or_empty_journals', (
    SELECT count(*) FROM (
      SELECT j.id
      FROM public.ledger_journals j
      LEFT JOIN public.ledger_journal_lines l ON l.journal_id=j.id
      GROUP BY j.id
      HAVING count(l.id) < 2
          OR coalesce(sum(l.debit),0) <> coalesce(sum(l.credit),0)
    ) invalid
  ),
  'invalid_journal_lines', (
    SELECT count(*) FROM public.ledger_journal_lines
    WHERE debit < 0 OR credit < 0 OR (debit > 0 AND credit > 0) OR (debit = 0 AND credit = 0)
  ),
  'orphan_journal_lines', (
    SELECT count(*) FROM public.ledger_journal_lines l
    WHERE NOT EXISTS (SELECT 1 FROM public.ledger_journals j WHERE j.id=l.journal_id)
  ),
  'duplicate_journal_idempotency_keys', (
    SELECT count(*) FROM (
      SELECT idempotency_key FROM public.ledger_journals
      GROUP BY idempotency_key HAVING count(*) > 1
    ) duplicate_keys
  ),
  'negative_ledger_entries', (
    SELECT count(*) FROM public.ledger_entries
    WHERE debit < 0 OR credit < 0 OR amount <= 0 OR (debit > 0 AND credit > 0) OR (debit = 0 AND credit = 0)
  ),
  'runtime_role', (
    SELECT jsonb_build_object(
      'name', r.rolname,
      'login', r.rolcanlogin,
      'inherit', r.rolinherit,
      'superuser', r.rolsuper,
      'create_role', r.rolcreaterole,
      'create_database', r.rolcreatedb,
      'replication', r.rolreplication,
      'bypass_rls', r.rolbypassrls,
      'owns_public_tables', (
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p') AND pg_get_userbyid(c.relowner)=r.rolname
      ),
      'role_memberships', (
        SELECT count(*) FROM pg_auth_members m WHERE m.member=r.oid
      ),
      'public_schema_usage', has_schema_privilege(r.rolname, 'public', 'USAGE'),
      'public_tables_missing_select', (
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
          AND NOT has_table_privilege(r.rolname,c.oid,'SELECT')
      ),
      'public_schema_create', has_schema_privilege(r.rolname, 'public', 'CREATE'),
      'public_tables_missing_dml', (
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
          AND NOT (has_table_privilege(r.rolname,c.oid,'INSERT')
               AND has_table_privilege(r.rolname,c.oid,'UPDATE')
               AND has_table_privilege(r.rolname,c.oid,'DELETE'))
      ),
      'public_sequences_missing_usage', (
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='S'
          AND NOT has_sequence_privilege(r.rolname,c.oid,'USAGE')
      )
    )
    FROM pg_roles r WHERE r.rolname=current_user
  )
)::text;
"""


def read_database_uri(value: str, staging_ref: str, production_ref: str) -> tuple[dict[str, str], str]:
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql", "postgresql+asyncpg", "postgresql+psycopg"} or not parsed.hostname:
        raise ValueError("DATABASE_URL must be a PostgreSQL URI")
    if not staging_ref or not re.fullmatch(r"[a-z0-9]{20}", staging_ref):
        raise ValueError("STAGING_SUPABASE_PROJECT_REF must be an explicit 20-character project ref")
    if not production_ref or not re.fullmatch(r"[a-z0-9]{20}", production_ref):
        raise ValueError("PRODUCTION_SUPABASE_PROJECT_REF must be an explicit 20-character project ref")
    if staging_ref == production_ref:
        raise ValueError("staging and production Supabase project refs must differ")
    allowed_hosts = {f"db.{staging_ref}.supabase.co", f"{staging_ref}.supabase.co"}
    if parsed.hostname.lower() not in allowed_hosts:
        raise ValueError("DATABASE_URL host does not match the explicitly configured staging Supabase project")
    if parsed.path.lstrip("/") != "postgres":
        raise ValueError("DATABASE_URL must target the expected postgres database")
    env = {key: val for key, val in os.environ.items() if key not in {"DATABASE_URL", "PGPASSWORD", "PGHOST", "PGPORT", "PGUSER", "PGDATABASE"}}
    env.update({
        "PGHOST": parsed.hostname,
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": unquote(parsed.username or ""),
        "PGPASSWORD": unquote(parsed.password or ""),
        "PGDATABASE": parsed.path.lstrip("/"),
        "PGSSLMODE": "require",
        "PGOPTIONS": "-c default_transaction_read_only=on -c statement_timeout=15000",
    })
    if not env["PGUSER"] or not env["PGPASSWORD"]:
        raise ValueError("DATABASE_URL must contain a database username and password")
    return env, parsed.hostname.lower()


def required_table_failures(value: object) -> list[str]:
    """Fail closed if restore SQL did not report required tables or found any missing."""
    if not isinstance(value, list):
        return ["required_tables_missing was not reported as a JSON array"]
    failures = []
    for table_name in value:
        if not isinstance(table_name, str) or not table_name.strip():
            failures.append("required_tables_missing contained a malformed table name")
        else:
            failures.append(f"required staging table missing: {table_name}")
    return failures


def truthy(value: object) -> bool:
    return value is True or str(value).strip().lower() in {"t", "true", "1", "yes"}


def audit(args: argparse.Namespace) -> dict:
    database_url = os.environ.get("DATABASE_URL", "")
    env, host = read_database_uri(
        database_url,
        os.environ.get("STAGING_SUPABASE_PROJECT_REF", ""),
        os.environ.get("PRODUCTION_SUPABASE_PROJECT_REF", ""),
    )
    if not args.restore_operation_id.strip():
        raise ValueError("A real backup restore operation ID is required")
    if not args.backup_reference.strip():
        raise ValueError("A backup source identifier/reference is required")
    evidence = str(args.restore_evidence_uri or "").strip()
    evidence_url = urlparse(evidence)
    if evidence_url.scheme not in {"https", "gs"} or not evidence_url.netloc:
        raise ValueError("A provider restore-operation evidence URI (https:// or gs://) is required")
    evidence_sha256 = str(args.restore_evidence_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256):
        raise ValueError("The downloaded provider restore-operation record must have a verified SHA-256")
    verified_target_ref = str(args.verified_target_project_ref or "").strip()
    if verified_target_ref != os.environ.get("STAGING_SUPABASE_PROJECT_REF", ""):
        raise ValueError("Provider restore-operation evidence was not verified against the configured staging project")
    proc = subprocess.run(
        ["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", QUERY],
        capture_output=True, text=True, env=env, check=False,
    )
    if proc.returncode:
        raise RuntimeError("Read-only SQL invariant checks failed; database output is withheld")
    try:
        observed = json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError("Read-only SQL audit returned invalid JSON") from exc

    role = observed.get("runtime_role") or {}
    failures = []
    expected_revision = args.expected_revision
    if observed.get("database") != "postgres":
        failures.append("unexpected database name")
    if observed.get("current_user") != observed.get("session_user"):
        failures.append("current_user differs from session_user")
    if role.get("name") != args.expected_runtime_role:
        failures.append("connected role differs from configured staging runtime role")
    if role.get("name") in {"postgres", "service_role", "supabase_admin", "supabase_auth_admin", "authenticator", "anon", "authenticated"}:
        failures.append("connected role is reserved or privileged")
    if not truthy(role.get("login")):
        failures.append("runtime role is not LOGIN")
    if truthy(role.get("inherit")):
        failures.append("runtime role must be NOINHERIT")
    for key in ("superuser", "create_role", "create_database", "replication"):
        if truthy(role.get(key)):
            failures.append(f"runtime role has forbidden capability {key}")
    if not truthy(role.get("bypass_rls")):
        failures.append("runtime role lacks the reviewed backend-only BYPASSRLS capability")
    if int(role.get("owns_public_tables", -1)) != 0:
        failures.append("runtime role owns application tables or owner count is unavailable")
    if int(role.get("role_memberships", -1)) != 0:
        failures.append("runtime role has inherited memberships or membership count is unavailable")
    if not truthy(role.get("public_schema_usage")):
        failures.append("runtime role lacks public schema USAGE")
    if truthy(role.get("public_schema_create")):
        failures.append("runtime role must not have CREATE on the public schema")
    for key in ("public_tables_missing_select", "public_tables_missing_dml", "public_sequences_missing_usage"):
        try:
            count = int(role.get(key, -1))
        except (TypeError, ValueError):
            count = -1
        if count != 0:
            failures.append(f"{key} expected 0, observed {count}")
    revision = str(observed.get("revision") or "")
    if revision != expected_revision:
        failures.append(f"restored database revision must be exactly {expected_revision}")
    if int(observed.get("public_table_count", 0)) < 1:
        failures.append("public schema has no tables")
    failures.extend(required_table_failures(observed.get("required_tables_missing")))
    for key in ("tables_without_rls", "negative_customer_ledger_buckets", "unbalanced_or_empty_journals",
                "invalid_journal_lines", "orphan_journal_lines", "duplicate_journal_idempotency_keys",
                "negative_ledger_entries"):
        try:
            count = int(observed.get(key, -1))
        except (TypeError, ValueError):
            count = -1
        if count != 0:
            failures.append(f"{key} expected 0, observed {count}")
    return {
        "schema_version": 1,
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "result": "PASS" if not failures else "FAIL",
        "restore_operation_id": args.restore_operation_id,
        "backup_reference": args.backup_reference,
        "restore_evidence_uri": evidence,
        "restore_evidence_sha256": evidence_sha256,
        "verified_restore_target_project_ref": verified_target_ref,
        "production_supabase_ref": os.environ["PRODUCTION_SUPABASE_PROJECT_REF"],
        "staging_supabase_ref": os.environ["STAGING_SUPABASE_PROJECT_REF"],
        "staging_database_host": host,
        "expected_runtime_role": args.expected_runtime_role,
        "expected_revision": expected_revision,
        "checks": observed,
        "failures": failures,
        "read_only": True,
        "restore_claim_scope": "Validates an already-restored target. Restore operation ID/source reference are supplied by operator and must be corroborated against the backup provider's own operation log.",
        "no_live_money_movement": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore-operation-id", required=True)
    parser.add_argument("--backup-reference", required=True, help="Provider backup ID or immutable backup object URI")
    parser.add_argument("--restore-evidence-uri", required=True, help="Provider restore operation evidence URL or immutable audit-log URI")
    parser.add_argument("--restore-evidence-sha256", required=True, help="SHA-256 of the downloaded provider-native JSON restore record")
    parser.add_argument("--verified-target-project-ref", required=True, help="Target ref verified from that provider record")
    parser.add_argument("--expected-runtime-role", required=True)
    parser.add_argument("--expected-revision", default="0051_research_trial_archive")
    parser.add_argument("--output", default="staging-restore-audit.json")
    args = parser.parse_args()
    try:
        report = audit(args)
        Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True, default=str, allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps({
            "result": report["result"],
            "restore_operation_id": report["restore_operation_id"],
            "staging_supabase_ref": report["staging_supabase_ref"],
            "revision": report["checks"].get("revision"),
            "failures": report["failures"],
            "report": args.output,
        }, indent=2))
        return 0 if report["result"] == "PASS" else 3
    except Exception as exc:
        print(f"ERROR: staging restore audit blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
