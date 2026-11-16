#!/usr/bin/env python3
"""Read-only integrity audit for a configured staging database and identified backup.

The free setup may use the existing Supabase project. This script only audits the
configured target and never creates/restores/migrates/drops data; never overwrite an
active source database to manufacture a restore result.
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

AUDIT_ROLE_NAME = "atlas_audit_reader"
AUDIT_REQUIRED_SELECT_TABLES = (
    "alembic_version",
    "customer_ledger_accounts",
    "ledger_entries",
    "ledger_journals",
    "ledger_journal_lines",
    "research_trial_archive",
    "research_trial_ledger_head",
    "research_holdout_registry",
    "withdrawals",
)


def audit_role_observations_sql() -> str:
    """Return catalog-only checks for the dedicated read-only audit login."""
    table_names = ", ".join(f"'{name}'" for name in AUDIT_REQUIRED_SELECT_TABLES)
    return f"""
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
        WHERE n.nspname='public' AND c.relkind IN ('r','p') AND c.relowner=r.oid
      ),
      'role_memberships', (
        SELECT count(*) FROM pg_auth_members m WHERE m.member=r.oid
      ),
      'public_schema_usage', has_schema_privilege(r.rolname,'public','USAGE'),
      'public_schema_create', has_schema_privilege(r.rolname,'public','CREATE'),
      'required_select_missing', (
        SELECT count(*)
        FROM unnest(ARRAY[{table_names}]::text[]) AS required(table_name)
        WHERE to_regclass(format('public.%I', required.table_name)) IS NULL
           OR NOT has_table_privilege(r.rolname,format('public.%I',required.table_name),'SELECT')
      ),
      'unexpected_select', (
        SELECT count(*)
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
          AND NOT (c.relname = ANY (ARRAY[{table_names}]::text[]))
          AND has_table_privilege(r.rolname,c.oid,'SELECT')
      ),
      'public_tables_with_write_privileges', (
        SELECT count(*)
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p') AND (
          has_table_privilege(r.rolname,c.oid,'INSERT')
          OR has_table_privilege(r.rolname,c.oid,'UPDATE')
          OR has_table_privilege(r.rolname,c.oid,'DELETE')
          OR has_table_privilege(r.rolname,c.oid,'TRUNCATE')
          OR has_table_privilege(r.rolname,c.oid,'REFERENCES')
          OR has_table_privilege(r.rolname,c.oid,'TRIGGER')
        )
      ),
      'public_sequences_with_privileges', (
        SELECT count(*)
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='S' AND (
          has_sequence_privilege(r.rolname,c.oid,'USAGE')
          OR has_sequence_privilege(r.rolname,c.oid,'SELECT')
          OR has_sequence_privilege(r.rolname,c.oid,'UPDATE')
        )
      ),
      'public_functions_with_execute', (
        SELECT count(*)
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND has_function_privilege(r.rolname,p.oid,'EXECUTE')
      )
    )
    FROM pg_roles r WHERE r.rolname=current_user
    """


def truthy(value: object) -> bool:
    return value is True or str(value).strip().lower() in {"t", "true", "1", "yes"}


def audit_role_failures(role: object, expected_role: str, transaction_read_only: object) -> list[str]:
    """Fail closed unless this is the exact, strictly read-only audit connection."""
    failures: list[str] = []
    if expected_role != AUDIT_ROLE_NAME:
        failures.append(f"expected audit role must be exactly {AUDIT_ROLE_NAME}")
    if not isinstance(role, dict):
        return failures + ["audit-role catalog evidence is missing or malformed"]
    if role.get("name") != expected_role:
        failures.append("connected role differs from the configured audit database role")
    if not truthy(role.get("login")):
        failures.append("audit role is not LOGIN")
    if truthy(role.get("inherit")):
        failures.append("audit role must be NOINHERIT")
    for key in ("superuser", "create_role", "create_database", "replication", "public_schema_create"):
        if truthy(role.get(key)):
            failures.append(f"audit role has forbidden capability: {key}")
    if not truthy(role.get("bypass_rls")) or not truthy(role.get("public_schema_usage")):
        failures.append("audit role lacks the reviewed backend-only BYPASSRLS/public-schema USAGE contract")
    for key in (
        "owns_public_tables",
        "role_memberships",
        "required_select_missing",
        "unexpected_select",
        "public_tables_with_write_privileges",
        "public_sequences_with_privileges",
        "public_functions_with_execute",
    ):
        try:
            observed = int(role.get(key, -1))
        except (TypeError, ValueError):
            observed = -1
        if observed != 0:
            failures.append(f"audit role {key} expected 0, observed {observed}")
    if not truthy(transaction_read_only):
        failures.append("audit connection is not running in a read-only PostgreSQL transaction")
    return failures


QUERY = f"""
SELECT jsonb_build_object(
  'database', current_database(),
  'current_user', current_user,
  'session_user', session_user,
  'transaction_read_only', current_setting('transaction_read_only'),
  'revision', (SELECT string_agg(version_num, ',' ORDER BY version_num) FROM public.alembic_version),
  'public_table_count', (
    SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relkind IN ('r','p')
  ),
  'required_tables_missing', (
    SELECT coalesce(jsonb_agg(required.table_name ORDER BY required.table_name), '[]'::jsonb)
    FROM unnest(ARRAY[
      'paper_order_states', 'paper_bar_states', 'paper_fill_events',
      'research_trial_archive', 'research_trial_ledger_head', 'research_holdout_registry'
    ]::text[]) AS required(table_name)
    WHERE to_regclass(format('public.%I', required.table_name)) IS NULL
  ),
  'research_trial_ledger_head_present', to_regclass('public.research_trial_ledger_head') IS NOT NULL,
  'research_holdout_registry_present', to_regclass('public.research_holdout_registry') IS NOT NULL,
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
  'audit_role', ({audit_role_observations_sql()})
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

    host = parsed.hostname.lower()
    direct_hosts = {f"db.{staging_ref}.supabase.co", f"{staging_ref}.supabase.co"}
    pooler_host = re.fullmatch(
        r"aws-\d+-[a-z0-9]+(?:-[a-z0-9]+)*\.pooler\.supabase\.com",
        host,
    )
    pooler_user = unquote(parsed.username or "")
    pooler_matches_project = bool(pooler_host and pooler_user.rpartition(".")[2] == staging_ref)
    if host not in direct_hosts and not pooler_matches_project:
        raise ValueError("DATABASE_URL host/user does not match the explicitly configured Supabase project")
    if parsed.port not in (None, 5432):
        raise ValueError("DATABASE_URL must use PostgreSQL port 5432")
    if parsed.path.lstrip("/") != "postgres":
        raise ValueError("DATABASE_URL must target the expected postgres database")

    env = {
        key: val for key, val in os.environ.items()
        if key not in {"DATABASE_URL", "PGPASSWORD", "PGHOST", "PGPORT", "PGUSER", "PGDATABASE"}
    }
    env.update({
        "PGHOST": host,
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": pooler_user,
        "PGPASSWORD": unquote(parsed.password or ""),
        "PGDATABASE": parsed.path.lstrip("/"),
        "PGSSLMODE": "require",
        "PGOPTIONS": "-c default_transaction_read_only=on -c statement_timeout=15000",
    })
    if not env["PGUSER"] or not env["PGPASSWORD"]:
        raise ValueError("DATABASE_URL must contain a database username and password")
    return env, host

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


RESEARCH_INTEGRITY_QUERY = r"""
SELECT jsonb_build_object(
  'research_trial_ledger_head_rows', (
    SELECT count(*) FROM public.research_trial_ledger_head
  ),
  'research_trial_ledger_head_valid', (
    SELECT count(*) = 1
       AND coalesce(bool_and(
         h.id = 1
         AND h.last_sequence = archive_state.last_sequence
         AND h.last_entry_sha256 = archive_state.last_entry_sha256
       ), false)
    FROM public.research_trial_ledger_head h
    CROSS JOIN LATERAL (
      SELECT
        coalesce(max(ledger_sequence), 0)::bigint AS last_sequence,
        coalesce(
          (
            SELECT a.ledger_entry_sha256
            FROM public.research_trial_archive a
            WHERE a.ledger_sequence = (
              SELECT coalesce(max(ledger_sequence), 0)
              FROM public.research_trial_archive
            )
          ),
          ''
        ) AS last_entry_sha256
      FROM public.research_trial_archive
    ) archive_state
  ),
  'research_holdout_registry_invalid_rows', (
    SELECT count(*)
    FROM public.research_holdout_registry
    WHERE scope_sha256 !~ '^[0-9a-f]{64}$'
       OR source_sample_sha256 !~ '^[0-9a-f]{64}$'
       OR report_sha256 !~ '^[0-9a-f]{64}$'
       OR holdout_version = ''
       OR holdout_plan_json = ''
  )
)::text;
"""

def add_research_integrity_observations(
    observed: dict,
    *,
    env: dict[str, str],
    run=subprocess.run,
) -> dict:
    """Fetch the integrity rows only after the base query confirms both relations exist.

    Keeping this as a separate read-only query avoids parse-time relation errors
    when auditing an older restored schema that has not applied migration 0059 yet.
    """
    present = (
        observed.get("research_trial_ledger_head_present") is True
        and observed.get("research_holdout_registry_present") is True
    )
    if not present:
        observed.update({
            "research_trial_ledger_head_rows": None,
            "research_trial_ledger_head_valid": False,
            "research_holdout_registry_invalid_rows": None,
        })
        return observed

    proc = run(
        ["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", RESEARCH_INTEGRITY_QUERY],
        capture_output=True, text=True, env=env, check=False,
    )
    if proc.returncode:
        raise RuntimeError("Read-only research-integrity SQL checks failed; database output is withheld")
    try:
        integrity = json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError("Research-integrity SQL audit returned invalid JSON") from exc
    expected = {
        "research_trial_ledger_head_rows",
        "research_trial_ledger_head_valid",
        "research_holdout_registry_invalid_rows",
    }
    if not isinstance(integrity, dict) or not expected.issubset(integrity):
        raise RuntimeError("Research-integrity SQL audit omitted required result fields")
    observed.update({key: integrity[key] for key in expected})
    return observed


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

    staging_ref = os.environ.get("STAGING_SUPABASE_PROJECT_REF", "")
    production_ref = os.environ.get("PRODUCTION_SUPABASE_PROJECT_REF", "")
    if staging_ref == production_ref:
        raise ValueError(
            "full restore audit requires a distinct non-production target; same-project restore could overwrite the active source"
        )
    failures = audit_role_failures(
        observed.get("audit_role"),
        args.expected_audit_role,
        observed.get("transaction_read_only"),
    )
    revision = str(observed.get("revision") or "")
    if revision != expected_revision:
        failures.append(f"restored database revision must be exactly {expected_revision}")
    if int(observed.get("public_table_count", 0)) < 1:
        failures.append("public schema has no tables")
    failures.extend(required_table_failures(observed.get("required_tables_missing")))
    try:
        head_rows = int(observed.get("research_trial_ledger_head_rows", -1))
    except (TypeError, ValueError):
        head_rows = -1
    if head_rows != 1:
        failures.append(f"research trial ledger head must contain exactly one singleton row, observed {head_rows}")
    if not truthy(observed.get("research_trial_ledger_head_valid")):
        failures.append("research trial ledger head does not match its last chained archive entry")
    try:
        invalid_holdout_rows = int(observed.get("research_holdout_registry_invalid_rows", -1))
    except (TypeError, ValueError):
        invalid_holdout_rows = -1
    if invalid_holdout_rows != 0:
        failures.append(f"research holdout registry contains invalid retained consumption rows: {invalid_holdout_rows}")
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
        "production_supabase_ref": os.environ.get("PRODUCTION_SUPABASE_PROJECT_REF", ""),
        "staging_supabase_ref": os.environ["STAGING_SUPABASE_PROJECT_REF"],
        "same_project_staging": (
            os.environ.get("STAGING_SUPABASE_PROJECT_REF", "")
            == os.environ.get("PRODUCTION_SUPABASE_PROJECT_REF", "")
        ),
        "staging_scope": "same_supabase_project" if (
            os.environ.get("STAGING_SUPABASE_PROJECT_REF", "")
            == os.environ.get("PRODUCTION_SUPABASE_PROJECT_REF", "")
        ) else "separate_supabase_project",
        "staging_database_host": host,
        "expected_audit_role": args.expected_audit_role,
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
    parser.add_argument("--expected-audit-role", "--expected-runtime-role", dest="expected_audit_role", required=True)
    parser.add_argument("--expected-revision", default="0059_research_integrity_controls")
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
            "expected_audit_role": report["expected_audit_role"],
            "failures": report["failures"],
            "report": args.output,
        }, indent=2))
        return 0 if report["result"] == "PASS" else 3
    except Exception as exc:
        print(f"ERROR: staging restore audit blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
