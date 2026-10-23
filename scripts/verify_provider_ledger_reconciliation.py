#!/usr/bin/env python3
"""Reconcile a provider payout statement to withdrawals and ledger journals (read-only).

The CSV is an authoritative provider statement filtered to Atlas withdrawal payouts.
It must include provider transaction ID, amount, currency, normalized/raw status and event time.
The script refuses empty or ambiguous evidence. It never calls a provider or mutates the database.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.verify_staging_restore import read_database_uri

COMPLETED = {"COMPLETED", "SUCCESS", "SUCCEEDED", "SETTLED", "PAID"}
FAILED = {"FAILED", "REJECTED", "CANCELLED", "CANCELED"}
PENDING = {"PENDING", "PROCESSING", "SUBMITTED", "IN_PROGRESS", "CONFIRMED", "QUEUED"}


def parse_utc(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp with timezone") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def status_kind(value: str) -> str:
    status = re.sub(r"[^A-Z_]", "", str(value or "").strip().upper())
    if status in COMPLETED:
        return "COMPLETED"
    if status in FAILED:
        return "FAILED"
    if status in PENDING:
        return "PENDING"
    return "UNKNOWN"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def short_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def load_provider_statement(path: Path) -> list[dict[str, str]]:
    required = {"provider_id", "amount", "currency", "status", "event_time_utc"}
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    with path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError("Provider statement is missing required columns: " + ", ".join(sorted(missing)))
        for line_no, raw in enumerate(reader, start=2):
            row = {k: str(v or "").strip() for k, v in raw.items() if k is not None}
            provider_id = row["provider_id"]
            if not provider_id or len(provider_id) > 180 or any(ord(ch) < 32 for ch in provider_id):
                raise ValueError(f"Invalid provider_id at CSV line {line_no}")
            if provider_id in seen:
                raise ValueError(f"Duplicate provider_id in provider statement at CSV line {line_no}")
            seen.add(provider_id)
            amount_text = row["amount"]
            try:
                amount = Decimal(amount_text)
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"Invalid amount at CSV line {line_no}") from exc
            if not amount.is_finite() or amount <= 0:
                raise ValueError(f"Provider amount must be finite and positive at CSV line {line_no}")
            row["amount"] = format(amount, "f")
            if not re.fullmatch(r"[A-Za-z]{3,12}", row["currency"]):
                raise ValueError(f"Invalid currency at CSV line {line_no}")
            if status_kind(row["status"]) == "UNKNOWN":
                raise ValueError(f"Unrecognized provider status at CSV line {line_no}")
            parse_utc(row["event_time_utc"], f"event_time_utc line {line_no}")
            rows.append(row)
    if not rows:
        raise ValueError("Provider statement contains no payout records; empty evidence cannot pass")
    return rows


def build_query(provider: str, from_utc: datetime, to_utc: datetime) -> str:
    # Provider and timestamps are validated and escaped before interpolating SQL literals.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", provider):
        raise ValueError("Invalid provider name")
    if from_utc.tzinfo is None or to_utc.tzinfo is None or from_utc >= to_utc:
        raise ValueError("A valid increasing timezone-aware timestamp window is required")
    provider_sql = provider.replace("'", "''")
    start = from_utc.isoformat(timespec="microseconds")
    end = to_utc.isoformat(timespec="microseconds")
    return f"""
    WITH matching_withdrawals AS (
      SELECT w.*
      FROM public.withdrawals w
      WHERE w.created_at >= TIMESTAMPTZ '{start}'
        AND w.created_at < TIMESTAMPTZ '{end}'
        AND (upper(w.provider) = upper('{provider_sql}') OR coalesce(w.provider,'') = '')
    )
    SELECT json_build_object(
      'database', current_database(),
      'current_user', current_user,
      'session_user', session_user,
      'revision', (SELECT string_agg(version_num, ',' ORDER BY version_num) FROM public.alembic_version),
      'public_table_count', (
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
      ),
      'tables_without_rls', (
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p') AND NOT c.relrowsecurity
      ),
      'role', (
        SELECT json_build_object(
          'name', r.rolname, 'login', r.rolcanlogin, 'inherit', r.rolinherit,
          'superuser', r.rolsuper, 'create_role', r.rolcreaterole,
          'create_database', r.rolcreatedb, 'replication', r.rolreplication,
          'bypass_rls', r.rolbypassrls,
          'owns_tables', (
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind IN ('r','p') AND pg_get_userbyid(c.relowner)=r.rolname
          ),
          'memberships', (SELECT count(*) FROM pg_auth_members m WHERE m.member=r.oid),
          'schema_usage', has_schema_privilege(r.rolname,'public','USAGE'),
          'schema_create', has_schema_privilege(r.rolname,'public','CREATE'),
          'tables_missing_select', (
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind IN ('r','p')
              AND NOT has_table_privilege(r.rolname,c.oid,'SELECT')
          ),
          'tables_missing_dml', (
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind IN ('r','p')
              AND NOT (has_table_privilege(r.rolname,c.oid,'INSERT')
                   AND has_table_privilege(r.rolname,c.oid,'UPDATE')
                   AND has_table_privilege(r.rolname,c.oid,'DELETE'))
          ),
          'sequences_missing_usage', (
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind='S'
              AND NOT has_sequence_privilege(r.rolname,c.oid,'USAGE')
          )
        ) FROM pg_roles r WHERE r.rolname=current_user
      ),
      'withdrawals', coalesce((
        SELECT json_agg(json_build_object(
          'request_id', w.request_id,
          'provider_id', w.provider_id,
          'provider', w.provider,
          'amount', w.amount::numeric::text,
          'currency', upper(w.currency),
          'status', upper(w.status),
          'provider_status', upper(w.provider_status),
          'reserve_count', (SELECT count(*) FROM public.ledger_journals j
            WHERE j.entry_type='WITHDRAWAL_RESERVE' AND j.idempotency_key='withdrawal-reserve:' || w.request_id),
          'settlement_count', (SELECT count(*) FROM public.ledger_journals j
            WHERE j.entry_type='WITHDRAWAL_SETTLEMENT' AND j.idempotency_key='withdrawal-settlement:' || w.request_id),
          'release_count', (SELECT count(*) FROM public.ledger_journals j
            WHERE j.entry_type='WITHDRAWAL_RELEASE' AND j.idempotency_key='withdrawal-release:' || w.request_id || ':release'),
          'journal_balance_failures', (SELECT count(*) FROM public.ledger_journals j
            WHERE j.reference_type='WITHDRAWAL' AND j.reference_id IN
              (w.request_id, w.request_id || ':release')
              AND (
                (SELECT count(*) FROM public.ledger_journal_lines l WHERE l.journal_id=j.id) < 2
                OR (SELECT coalesce(sum(l.debit),0) FROM public.ledger_journal_lines l WHERE l.journal_id=j.id)
                   <> (SELECT coalesce(sum(l.credit),0) FROM public.ledger_journal_lines l WHERE l.journal_id=j.id)
                OR EXISTS (SELECT 1 FROM public.ledger_journal_lines l WHERE l.journal_id=j.id
                           AND (l.debit < 0 OR l.credit < 0 OR (l.debit=0 AND l.credit=0)
                                OR (l.debit>0 AND l.credit>0)))
              ))
        ) ORDER BY w.created_at)
        FROM matching_withdrawals w
      ), '[]'::json)
    )::text;
    """


def reconcile_rows(
    statement_rows: list[dict[str, str]],
    database_rows: list[dict[str, Any]],
    provider: str,
) -> tuple[int, int, list[str]]:
    """Compare provider payout rows to DB withdrawals and their terminal ledger journals."""
    failures: list[str] = []
    provider_rows = {row["provider_id"]: row for row in statement_rows}
    if not database_rows:
        failures.append("no Atlas withdrawals found in the specified created_at window")
    db_by_id: dict[str, dict[str, Any]] = {}
    unresolved_no_id = 0
    for record in database_rows:
        provider_id = str(record.get("provider_id") or "")
        internal = str(record.get("status") or "").upper()
        if not provider_id:
            if internal in {"SUBMITTING", "SUBMITTED", "UNKNOWN"}:
                unresolved_no_id += 1
                failures.append("ambiguous withdrawal has no provider_id")
            else:
                failures.append("withdrawal has no provider_id and cannot be reconciled")
            continue
        if provider_id in db_by_id:
            failures.append("duplicate provider_id in staging withdrawals")
        db_by_id[provider_id] = record

    for provider_id in provider_rows.keys() - db_by_id.keys():
        failures.append("provider statement contains an unmatched provider_id:" + short_hash(provider_id))
    for provider_id in db_by_id.keys() - provider_rows.keys():
        failures.append("staging withdrawal missing from provider statement:" + short_hash(provider_id))

    matched = 0
    for provider_id in provider_rows.keys() & db_by_id.keys():
        statement = provider_rows[provider_id]
        record = db_by_id[provider_id]
        matched += 1
        if str(record.get("provider") or "").upper() != provider.upper():
            failures.append("provider name mismatch:" + short_hash(provider_id))
        try:
            db_amount = Decimal(str(record["amount"]))
            provider_amount = Decimal(statement["amount"])
            if not db_amount.is_finite() or not provider_amount.is_finite() or db_amount != provider_amount:
                failures.append("amount mismatch:" + short_hash(provider_id))
        except (InvalidOperation, KeyError, ValueError):
            failures.append("invalid amount in staging row:" + short_hash(provider_id))
        if str(record.get("currency") or "").upper() != statement["currency"].upper():
            failures.append("currency mismatch:" + short_hash(provider_id))

        db_kind = status_kind(str(record.get("provider_status") or ""))
        ext_kind = status_kind(statement["status"])
        if db_kind == "UNKNOWN" or db_kind != ext_kind:
            failures.append("provider status mismatch/unknown:" + short_hash(provider_id))

        try:
            reserve_count = int(record.get("reserve_count") or 0)
            settlement_count = int(record.get("settlement_count") or 0)
            release_count = int(record.get("release_count") or 0)
            journal_balance_failures = int(record.get("journal_balance_failures") or 0)
        except (TypeError, ValueError):
            failures.append("invalid ledger journal evidence:" + short_hash(provider_id))
            continue

        if reserve_count != 1:
            failures.append("withdrawal must have exactly one reserve journal:" + short_hash(provider_id))
        if journal_balance_failures:
            failures.append("withdrawal journal is unbalanced or has fewer than two lines:" + short_hash(provider_id))

        internal_status = str(record.get("status") or "").upper()
        if ext_kind == "COMPLETED":
            if internal_status not in {"RELEASED", "COMPLETED"} or settlement_count != 1 or release_count != 0:
                failures.append("completed payout lacks exactly one settlement or has a release journal:" + short_hash(provider_id))
        elif ext_kind == "FAILED":
            if internal_status not in {"FAILED", "REJECTED", "CANCELLED", "CANCELED"} or release_count != 1 or settlement_count != 0:
                failures.append("failed payout lacks exactly one reserve release or has a settlement journal:" + short_hash(provider_id))
        elif ext_kind == "PENDING":
            if internal_status not in {"SUBMITTING", "SUBMITTED", "UNKNOWN"} or settlement_count != 0 or release_count != 0:
                failures.append("pending/ambiguous payout has an invalid terminal state or journal:" + short_hash(provider_id))
    return matched, unresolved_no_id, failures


def audit(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.statement_csv)
    provider = args.provider.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", provider):
        raise ValueError("Provider name must contain only letters, numbers, underscore or hyphen")
    start = parse_utc(args.from_utc, "from_utc")
    end = parse_utc(args.to_utc, "to_utc")
    if start >= end:
        raise ValueError("from_utc must be earlier than to_utc")
    rows = load_provider_statement(path)
    if not args.expected_runtime_role.strip():
        raise ValueError("An explicit staging runtime role is required")

    env, host = read_database_uri(
        args.database_url,
        args.staging_ref,
        args.production_ref,
    )
    proc = subprocess.run(
        ["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", build_query(provider, start, end)],
        capture_output=True, text=True, env=env, check=False,
    )
    if proc.returncode:
        raise RuntimeError("Read-only staging/provider reconciliation query failed; database output is withheld")
    try:
        db = json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError("Database reconciliation query did not return JSON") from exc

    failures: list[str] = []
    if db.get("database") != "postgres" or db.get("current_user") != db.get("session_user"):
        failures.append("unexpected database/session identity")
    if str(db.get("revision") or "") != args.expected_revision:
        failures.append("staging schema revision differs from expected revision")
    if int(db.get("public_table_count") or 0) < 1 or int(db.get("tables_without_rls") or -1) != 0:
        failures.append("public schema RLS verification failed")

    role = db.get("role") or {}
    if role.get("name") != args.expected_runtime_role:
        failures.append("runtime role name mismatch")
    for key in ("superuser", "create_role", "create_database", "replication", "inherit", "schema_create"):
        if role.get(key) is True:
            failures.append(f"runtime role has forbidden capability: {key}")
    if role.get("login") is not True or role.get("bypass_rls") is not True or role.get("schema_usage") is not True:
        failures.append("runtime role login/schema/bypass capabilities do not meet policy")
    for key in ("owns_tables", "memberships", "tables_missing_select", "tables_missing_dml", "sequences_missing_usage"):
        try:
            count = int(role.get(key, -1))
        except (TypeError, ValueError):
            count = -1
        if count != 0:
            failures.append(f"runtime role {key} expected 0, observed {count}")

    db_rows = db.get("withdrawals") or []
    matched, unresolved_no_id, reconciliation_failures = reconcile_rows(rows, db_rows, provider)
    failures.extend(reconciliation_failures)

    return {
        "schema_version": 1,
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "result": "PASS" if not failures else "FAIL",
        "provider": provider,
        "from_utc": start.isoformat(),
        "to_utc": end.isoformat(),
        "database_host": host,
        "staging_project_ref": args.staging_ref,
        "production_project_ref": args.production_ref,
        "same_project_staging": args.staging_ref == args.production_ref,
        "staging_scope": "same_supabase_project" if args.staging_ref == args.production_ref else "separate_supabase_project",
        "expected_runtime_role": args.expected_runtime_role,
        "expected_revision": args.expected_revision,
        "provider_statement_sha256": sha256_file(path),
        "statement_records": len(rows),
        "staging_withdrawal_records": len(db_rows),
        "matched_records": matched,
        "unresolved_without_provider_id": unresolved_no_id,
        "failures": failures,
        "identifier_handling": "IDs are SHA-256 truncated in discrepancy messages; raw customer/profile fields are never queried.",
        "read_only": True,
        "no_provider_call_or_money_movement": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--statement-csv", required=True, help="Provider statement filtered to Atlas payout transactions")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--from-utc", required=True, help="UTC window for internal withdrawal creation, ISO-8601")
    parser.add_argument("--to-utc", required=True, help="Exclusive UTC window for internal withdrawal creation, ISO-8601")
    parser.add_argument("--database-url", default="", help="Read-only direct staging DB URL; alternatively use DATABASE_URL")
    parser.add_argument("--staging-ref", default="")
    parser.add_argument("--production-ref", default="")
    parser.add_argument("--expected-runtime-role", required=True)
    parser.add_argument("--expected-revision", default="0053_supabase_public_default_acl_lockdown")
    parser.add_argument("--output", default="provider-ledger-reconciliation.json")
    args = parser.parse_args()
    import os
    args.database_url = args.database_url or os.environ.get("DATABASE_URL", "")
    args.staging_ref = args.staging_ref or os.environ.get("STAGING_SUPABASE_PROJECT_REF", "")
    args.production_ref = args.production_ref or os.environ.get("PRODUCTION_SUPABASE_PROJECT_REF", "")
    try:
        report = audit(args)
        Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps({
            "result": report["result"],
            "provider": report["provider"],
            "records": report["statement_records"],
            "matched": report["matched_records"],
            "failures": len(report["failures"]),
            "provider_statement_sha256": report["provider_statement_sha256"],
            "report": args.output,
        }, indent=2))
        return 0 if report["result"] == "PASS" else 3
    except Exception as exc:
        print(f"ERROR: provider-to-ledger reconciliation blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
