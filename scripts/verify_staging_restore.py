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
AUDIT_REQUIRED_SELECT_COLUMNS = {
    "alembic_version": ("version_num",),
    "customer_ledger_accounts": ("available", "trading_reserved", "withdrawal_reserved"),
    "ledger_entries": ("debit", "credit", "amount"),
    "ledger_journals": ("id", "entry_type", "idempotency_key", "reference_type", "reference_id", "created_at"),
    "ledger_journal_lines": ("id", "journal_id", "debit", "credit"),
    "research_trial_archive": ("ledger_sequence", "ledger_entry_sha256"),
    "research_trial_ledger_head": ("id", "last_sequence", "last_entry_sha256"),
    "research_holdout_registry": (
        "scope_sha256", "source_sample_sha256", "report_sha256", "holdout_version", "holdout_plan_json"
    ),
    "withdrawals": (
        "created_at", "request_id", "provider_id", "provider", "amount", "currency", "status", "provider_status"
    ),
}
AUDIT_REQUIRED_SELECT_TABLES = tuple(AUDIT_REQUIRED_SELECT_COLUMNS)


def audit_role_observations_sql() -> str:
    """Return catalog-only checks for the dedicated read-only audit login."""
    column_pairs = ", ".join(
        f"('{table_name}', '{column_name}')"
        for table_name, columns in AUDIT_REQUIRED_SELECT_COLUMNS.items()
        for column_name in columns
    )
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
      'role_default_read_only', COALESCE((
        SELECT lower(split_part(cfg.setting, '=', 2)) IN ('on', 'true', '1')
        FROM pg_db_role_setting s
        CROSS JOIN LATERAL unnest(s.setconfig) AS cfg(setting)
        WHERE s.setrole = r.oid
          AND s.setdatabase IN (
            0,
            (SELECT oid FROM pg_database WHERE datname = current_database())
          )
          AND lower(split_part(cfg.setting, '=', 1)) = 'default_transaction_read_only'
        ORDER BY (
          s.setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database())
        ) DESC
        LIMIT 1
      ), false),
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
        FROM (VALUES {column_pairs}) AS required(table_name,column_name)
        LEFT JOIN pg_class c
          ON c.relnamespace = 'public'::regnamespace
         AND c.relname = required.table_name
         AND c.relkind IN ('r','p')
        LEFT JOIN pg_attribute a
          ON a.attrelid = c.oid
         AND a.attname = required.column_name
         AND a.attnum > 0
         AND NOT a.attisdropped
        WHERE a.attnum IS NULL
           OR NOT has_column_privilege(r.rolname,c.oid,a.attnum,'SELECT')
      ),
      'unexpected_select', (
        SELECT count(*)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
          AND a.attnum > 0 AND NOT a.attisdropped
          AND has_column_privilege(r.rolname,c.oid,a.attnum,'SELECT')
          AND NOT EXISTS (
            SELECT 1 FROM (VALUES {column_pairs}) AS required(table_name,column_name)
            WHERE required.table_name=c.relname AND required.column_name=a.attname
          )
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
          OR EXISTS (
            SELECT 1 FROM pg_attribute a
            WHERE a.attrelid=c.oid AND a.attnum > 0 AND NOT a.attisdropped AND (
              has_column_privilege(r.rolname,c.oid,a.attnum,'INSERT')
              OR has_column_privilege(r.rolname,c.oid,a.attnum,'UPDATE')
              OR has_column_privilege(r.rolname,c.oid,a.attnum,'REFERENCES')
            )
          )
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
    return value is True or str(value).strip().lower() in {"t", "true", "1", "yes", "on"}


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
    if not truthy(role.get("role_default_read_only")):
        failures.append("audit role default_transaction_read_only is not enabled at role/database level")
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
  'customer_paper_schema_present', to_regnamespace('customer_paper') IS NOT NULL,
  'customer_paper_tables_missing', (
    SELECT coalesce(jsonb_agg(required.table_name ORDER BY required.table_name), '[]'::jsonb)
    FROM unnest(ARRAY[
      'accounts', 'journals', 'journal_lines', 'orders', 'fills',
      'order_events', 'position_events', 'market_bar_budgets', 'market_bar_allocations'
    ]::text[]) AS required(table_name)
    WHERE to_regclass(format('customer_paper.%I', required.table_name)) IS NULL
  ),
  'customer_paper_tables_without_forced_rls', (
    SELECT count(*)
    FROM unnest(ARRAY[
      'accounts', 'journals', 'journal_lines', 'orders', 'fills',
      'order_events', 'position_events', 'market_bar_budgets', 'market_bar_allocations'
    ]::text[]) AS required(table_name)
    LEFT JOIN pg_class c
      ON c.oid = to_regclass(format('customer_paper.%I', required.table_name))
    WHERE c.oid IS NULL OR NOT c.relrowsecurity OR NOT c.relforcerowsecurity
  ),
  'customer_paper_policy_count', (
    SELECT count(*) FROM pg_policies WHERE schemaname = 'customer_paper'
  ),
  'customer_paper_tenant_policy_count', (
    SELECT count(*) FROM pg_policies
    WHERE schemaname = 'customer_paper'
      AND policyname = 'customer_paper_tenant'
      AND tablename IN (
        'accounts', 'journals', 'journal_lines', 'orders', 'fills',
        'order_events', 'position_events'
      )
  ),
  'customer_paper_backend_policies', (
    SELECT coalesce(jsonb_agg(jsonb_build_object(
      'table', p.tablename,
      'cmd', p.cmd,
      'permissive', p.permissive,
      'roles', to_jsonb(p.roles),
      'qual', p.qual,
      'with_check', p.with_check
    ) ORDER BY p.tablename), '[]'::jsonb)
    FROM pg_policies p
    WHERE p.schemaname = 'customer_paper'
      AND p.policyname = 'customer_paper_bar_budget_backend'
  ),
  'customer_paper_tenant_policies', (
    SELECT coalesce(jsonb_agg(jsonb_build_object(
      'table', p.tablename,
      'cmd', p.cmd,
      'permissive', p.permissive,
      'roles', to_jsonb(p.roles),
      'qual', p.qual,
      'with_check', p.with_check
    ) ORDER BY p.tablename), '[]'::jsonb)
    FROM pg_policies p
    WHERE p.schemaname = 'customer_paper'
      AND p.policyname = 'customer_paper_tenant'
  ),
  'customer_paper_backend_policy_count', (
    SELECT count(*) FROM pg_policies
    WHERE schemaname = 'customer_paper'
      AND policyname = 'customer_paper_bar_budget_backend'
      AND tablename IN ('market_bar_budgets', 'market_bar_allocations')
  ),
  'customer_paper_wrong_policy_role_count', (
    SELECT count(*)
    FROM pg_policies
    WHERE schemaname = 'customer_paper'
      AND (
        (policyname = 'customer_paper_tenant'
          AND tablename IN (
            'accounts', 'journals', 'journal_lines', 'orders', 'fills',
            'order_events', 'position_events'
          )
          AND roles <> ARRAY['atlas_customer_paper']::name[])
        OR
        (policyname = 'customer_paper_bar_budget_backend'
          AND tablename IN ('market_bar_budgets', 'market_bar_allocations')
          AND roles <> ARRAY['atlas_customer_paper']::name[])
      )
  ),
  'customer_paper_runtime_role_valid', EXISTS (
    SELECT 1 FROM pg_roles r
    WHERE r.rolname = 'atlas_customer_paper'
      AND r.rolcanlogin
      AND NOT r.rolsuper
      AND NOT r.rolcreaterole
      AND NOT r.rolcreatedb
      AND NOT r.rolreplication
      AND NOT r.rolinherit
      AND NOT r.rolbypassrls
      AND NOT EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid)
      AND (
        SELECT count(*) FROM pg_auth_members m WHERE m.roleid = r.oid
      ) <= 1
      AND NOT EXISTS (
        SELECT 1
        FROM pg_auth_members m
        JOIN pg_roles member_role ON member_role.oid = m.member
        JOIN pg_roles grantor_role ON grantor_role.oid = m.grantor
        WHERE m.roleid = r.oid
          AND NOT (
            member_role.rolname = 'postgres'
            AND (
              (
                COALESCE(current_setting('atlas.restore_target_kind', true), 'supabase') = 'supabase'
                AND grantor_role.rolname = 'supabase_admin'
              )
              OR
              (
                current_setting('atlas.restore_target_kind', true) = 'cloud_sql'
                AND grantor_role.rolname = 'cloudsqladmin'
              )
            )
            AND m.admin_option
            AND (to_jsonb(m) ? 'inherit_option')
            AND COALESCE((to_jsonb(m)->>'inherit_option')::boolean, false) = false
            AND (to_jsonb(m) ? 'set_option')
            AND COALESCE((to_jsonb(m)->>'set_option')::boolean, false) = false
          )
      )
      AND NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'customer_paper'
          AND c.relkind IN ('r', 'p')
          AND c.relowner = r.oid
      )
  ),
  'customer_paper_runtime_schema_usage', CASE
    WHEN to_regrole('atlas_customer_paper') IS NULL
      OR to_regnamespace('customer_paper') IS NULL THEN false
    ELSE has_schema_privilege('atlas_customer_paper', 'customer_paper', 'USAGE')
  END,
  'customer_paper_runtime_schema_create', CASE
    WHEN to_regrole('atlas_customer_paper') IS NULL
      OR to_regnamespace('customer_paper') IS NULL THEN true
    ELSE has_schema_privilege('atlas_customer_paper', 'customer_paper', 'CREATE')
  END,
  'customer_paper_runtime_public_schema_usage', CASE
    WHEN to_regrole('atlas_customer_paper') IS NULL THEN true
    ELSE has_schema_privilege('atlas_customer_paper', 'public', 'USAGE')
  END,
  'customer_paper_runtime_invalid_table_privileges', (
    SELECT count(*)
    FROM unnest(ARRAY[
      'accounts', 'journals', 'journal_lines', 'orders', 'fills',
      'order_events', 'position_events', 'market_bar_budgets', 'market_bar_allocations'
    ]::text[]) AS required(table_name)
    LEFT JOIN pg_class c
      ON c.oid = to_regclass(format('customer_paper.%I', required.table_name))
    LEFT JOIN pg_roles runtime_role
      ON runtime_role.rolname = 'atlas_customer_paper'
    WHERE c.oid IS NULL OR runtime_role.oid IS NULL
       OR CASE
            WHEN c.oid IS NOT NULL AND runtime_role.oid IS NOT NULL THEN (
              NOT has_table_privilege(runtime_role.oid, c.oid, 'SELECT')
              OR NOT has_table_privilege(runtime_role.oid, c.oid, 'INSERT')
              OR has_table_privilege(runtime_role.oid, c.oid, 'UPDATE')
              OR has_table_privilege(runtime_role.oid, c.oid, 'DELETE')
              OR has_table_privilege(runtime_role.oid, c.oid, 'TRUNCATE')
              OR has_table_privilege(runtime_role.oid, c.oid, 'REFERENCES')
              OR has_table_privilege(runtime_role.oid, c.oid, 'TRIGGER')
              OR CASE
                   WHEN current_setting('server_version_num')::integer >= 170000
                   THEN has_table_privilege(runtime_role.oid, c.oid, 'MAINTAIN')
                   ELSE false
                 END
            )
            ELSE false
          END
  ),
  'customer_paper_runtime_sequences', (
    SELECT count(*)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'customer_paper'
      AND c.relkind = 'S'
  ),
  'customer_paper_runtime_sequences_with_usage', (
    SELECT count(*)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'customer_paper'
      AND c.relkind = 'S'
      AND CASE
            WHEN c.relkind = 'S' AND to_regrole('atlas_customer_paper') IS NOT NULL
            THEN has_sequence_privilege('atlas_customer_paper', c.oid, 'USAGE')
            ELSE false
          END
  ),
  'customer_paper_runtime_sequences_with_forbidden_privileges', (
    SELECT count(*)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'customer_paper'
      AND c.relkind = 'S'
      AND CASE
            WHEN c.relkind = 'S' AND to_regrole('atlas_customer_paper') IS NOT NULL
            THEN has_sequence_privilege('atlas_customer_paper', c.oid, 'UPDATE')
            ELSE false
          END
  ),
  'customer_paper_runtime_public_financial_privileges', (
    SELECT CASE
      WHEN to_regrole('atlas_customer_paper') IS NULL THEN 0
      ELSE (
        SELECT count(*)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p')
          AND c.relname IN (
            'customer_ledger_accounts', 'ledger_entries', 'ledger_journals',
            'ledger_journal_lines', 'withdrawals', 'wallets',
            'funding_transactions', 'tron_sweeps', 'trades', 'positions'
          )
          AND (
            has_table_privilege('atlas_customer_paper', c.oid, 'SELECT')
            OR has_table_privilege('atlas_customer_paper', c.oid, 'INSERT')
            OR has_table_privilege('atlas_customer_paper', c.oid, 'UPDATE')
            OR has_table_privilege('atlas_customer_paper', c.oid, 'DELETE')
            OR has_table_privilege('atlas_customer_paper', c.oid, 'TRUNCATE')
            OR has_table_privilege('atlas_customer_paper', c.oid, 'REFERENCES')
            OR has_table_privilege('atlas_customer_paper', c.oid, 'TRIGGER')
            OR CASE
                 WHEN current_setting('server_version_num')::integer >= 170000
                 THEN has_table_privilege('atlas_customer_paper', c.oid, 'MAINTAIN')
                 ELSE false
               END
          )
      )
    END
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

def validate_cloud_sql_target_pair(target_instance: str, production_instance: str) -> None:
    """Require explicit, valid, different Cloud SQL recovery and production identities."""
    pattern = r"^[a-z0-9-]+:[a-z0-9-]+:[a-z0-9-]+$"
    if not re.fullmatch(pattern, target_instance or ""):
        raise ValueError("CLOUD_SQL_INSTANCE_CONNECTION_NAME must be project:region:instance")
    if not re.fullmatch(pattern, production_instance or ""):
        raise ValueError(
            "PRODUCTION_CLOUD_SQL_INSTANCE_CONNECTION_NAME must be explicitly configured as project:region:instance"
        )
    if target_instance == production_instance:
        raise ValueError("Cloud SQL restore target must be distinct from the production instance")


def validate_cloud_sql_target_for_supabase_source(
    target_instance: str,
    source_supabase_project_ref: str,
) -> None:
    """Validate a Cloud SQL recovery target for the separately evidenced Supabase source."""
    match = re.fullmatch(r"^([a-z0-9-]+):([a-z0-9-]+):([a-z0-9-]+)$", target_instance or "")
    if match is None:
        raise ValueError("CLOUD_SQL_INSTANCE_CONNECTION_NAME must be project:region:instance")
    target_project, target_region, _instance_name = match.groups()
    if target_project != "atlas-staging-510915" or target_region != "europe-west1":
        raise ValueError(
            "Cloud SQL recovery target must be an isolated instance in atlas-staging-510915:europe-west1"
        )
    if source_supabase_project_ref != "wauaelzyugjuepmrewaw":
        raise ValueError(
            "SOURCE_SUPABASE_PROJECT_REF must match the authenticated October 22 pg_dump evidence"
        )


def read_database_uri(value: str, staging_ref: str = "", production_ref: str = "", *, target_kind: str = "supabase", cloud_sql_instance: str = "", cloud_sql_database: str = "") -> tuple[dict[str, str], str]:
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql", "postgresql+asyncpg", "postgresql+psycopg"} or not parsed.hostname:
        raise ValueError("DATABASE_URL must be a PostgreSQL URI")
    host = parsed.hostname.lower()
    database = parsed.path.lstrip("/")
    if target_kind == "supabase":
        if not staging_ref or not re.fullmatch(r"[a-z0-9]{20}", staging_ref):
            raise ValueError("STAGING_SUPABASE_PROJECT_REF must be an explicit 20-character project ref")
        if not production_ref or not re.fullmatch(r"[a-z0-9]{20}", production_ref):
            raise ValueError("PRODUCTION_SUPABASE_PROJECT_REF must be an explicit 20-character project ref")
        direct_hosts = {f"db.{staging_ref}.supabase.co", f"{staging_ref}.supabase.co"}
        pooler_host = re.fullmatch(r"aws-\d+-[a-z0-9]+(?:-[a-z0-9]+)*\.pooler\.supabase\.com", host)
        pooler_user = unquote(parsed.username or "")
        pooler_matches_project = bool(pooler_host and pooler_user.rpartition(".")[2] == staging_ref)
        if host not in direct_hosts and not pooler_matches_project:
            raise ValueError("DATABASE_URL host/user does not match the explicitly configured Supabase project")
        if database != "postgres":
            raise ValueError("DATABASE_URL must target the expected postgres database")
    elif target_kind == "cloud_sql":
        if not re.fullmatch(r"[a-z0-9-]+:[a-z0-9-]+:[a-z0-9-]+", cloud_sql_instance or ""):
            raise ValueError("CLOUD_SQL_INSTANCE_CONNECTION_NAME must be project:region:instance")
        if not cloud_sql_database or database != cloud_sql_database:
            raise ValueError("DATABASE_URL database does not match the configured Cloud SQL database")
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("Cloud SQL audit DATABASE_URL must connect through the local Cloud SQL Auth Proxy")
    else:
        raise ValueError("RESTORE_TARGET_KIND must be supabase or cloud_sql")
    if parsed.port not in (None, 5432):
        raise ValueError("DATABASE_URL must use PostgreSQL port 5432")
    env = {
        key: val for key, val in os.environ.items()
        if key not in {"DATABASE_URL", "PGPASSWORD", "PGHOST", "PGPORT", "PGUSER", "PGDATABASE"}
    }
    env.update({
        "PGHOST": host,
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": unquote(parsed.username or ""),
        "PGPASSWORD": unquote(parsed.password or ""),
        "PGDATABASE": database,
        "PGSSLMODE": "disable" if target_kind == "cloud_sql" else "require",
        "PGOPTIONS": (
            "-c default_transaction_read_only=on "
            f"-c statement_timeout=15000 -c atlas.restore_target_kind={target_kind}"
        ),
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


CUSTOMER_PAPER_TENANT_TABLES = (
    "accounts", "journals", "journal_lines", "orders", "fills", "order_events", "position_events",
)
CUSTOMER_PAPER_TENANT_ROLE = "atlas_customer_paper"
_TENANT_PREDICATE_REFERENCE = (
    "customer_id = (SELECT NULLIF(current_setting('app.customer_id', true), '')::bigint)"
)


def _canonical_policy_expression(expr: object) -> str:
    """Normalize PostgreSQL deparser wrappers without rewriting quoted GUC names.

    String literals remain intact. Only the SELECT keyword, PostgreSQL's injected
    text casts, the generated NULLIF alias, and expression-wrapping parentheses
    are removed before comparing tokens.
    """
    source = str(expr or "")
    tokens = re.findall(
        r"""'(?:''|[^'])*'|"(?:""|[^"])*"|::[A-Za-z_][A-Za-z0-9_$]*|[A-Za-z_][A-Za-z0-9_$]*|[0-9]+|[(),=.]|[^\s]""",
        source,
    )
    normalized: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.casefold()
        if lowered in {"select", "(", ")"} or lowered == "::text":
            index += 1
            continue
        if lowered == "as" and index + 1 < len(tokens):
            alias = tokens[index + 1].strip('"').casefold()
            if alias in {"nullif", "current_setting"}:
                index += 2
                continue
        if token.startswith("'"):
            normalized.append(token)
        elif token.startswith('"'):
            normalized.append(token[1:-1].replace('""', '"').casefold())
        else:
            normalized.append(lowered)
        index += 1
    return "".join(normalized)


_TENANT_PREDICATE_CANONICAL = _canonical_policy_expression(_TENANT_PREDICATE_REFERENCE)


def customer_paper_tenant_policy_failures(policies: object) -> list[str]:
    """Require the reviewed customer-id policy on each tenant-owned customer-paper table."""
    if not isinstance(policies, list):
        return ["customer_paper_tenant_policies was not reported as a JSON array"]
    failures: list[str] = []
    seen_tables: list[str] = []
    for policy in policies:
        if not isinstance(policy, dict):
            failures.append("customer_paper_tenant policy observation is malformed")
            continue
        table_value = policy.get("table")
        table = table_value if isinstance(table_value, str) else ""
        seen_tables.append(table)
        problems: list[str] = []
        if policy.get("cmd") != "ALL":
            problems.append("command is not ALL")
        if policy.get("permissive") != "PERMISSIVE":
            problems.append("policy is not PERMISSIVE")
        if policy.get("roles") != [CUSTOMER_PAPER_TENANT_ROLE]:
            problems.append(f"roles are not exactly {CUSTOMER_PAPER_TENANT_ROLE}")
        if _canonical_policy_expression(policy.get("qual")) != _TENANT_PREDICATE_CANONICAL:
            problems.append("USING expression is not the tenant predicate")
        if _canonical_policy_expression(policy.get("with_check")) != _TENANT_PREDICATE_CANONICAL:
            problems.append("WITH CHECK expression is not the tenant predicate")
        failures.extend(
            f"customer_paper_tenant policy on {table or '<unknown>'}: {problem}"
            for problem in problems
        )
    if sorted(seen_tables) != sorted(CUSTOMER_PAPER_TENANT_TABLES):
        failures.append("customer_paper_tenant policies do not cover exactly the expected tables")
    return failures


def customer_paper_backend_policy_failures(policies: object) -> list[str]:
    """Require shared market-bar policies to remain scoped to the paper backend role."""
    expected_tables = {"market_bar_budgets", "market_bar_allocations"}
    if not isinstance(policies, list):
        return ["customer_paper_backend_policies was not reported as a JSON array"]

    failures: list[str] = []
    seen: set[str] = set()
    for policy in policies:
        if not isinstance(policy, dict):
            failures.append("customer_paper backend policy observation is malformed")
            continue
        table_value = policy.get("table")
        table = table_value if isinstance(table_value, str) else ""
        if table not in expected_tables:
            failures.append(f"customer_paper backend policy references unexpected table: {table!r}")
            continue
        if table in seen:
            failures.append(f"customer_paper.{table} has duplicate backend policy observations")
            continue
        seen.add(table)

        roles = policy.get("roles")
        if (
            policy.get("cmd") != "ALL"
            or policy.get("permissive") != "PERMISSIVE"
            or roles != [CUSTOMER_PAPER_TENANT_ROLE]
            or _canonical_policy_expression(policy.get("qual")) != "true"
            or _canonical_policy_expression(policy.get("with_check")) != "true"
        ):
            failures.append(
                f"customer_paper.{table} backend policy role, command, permissiveness, or predicate is unsafe"
            )
    if seen != expected_tables:
        failures.append("customer_paper backend policies do not cover exactly the expected tables")
    return failures


def customer_paper_schema_failures(observed: object) -> list[str]:
    """Require all customer-paper tables, exact tenant policy definitions, and least-privilege grants."""
    if not isinstance(observed, dict):
        return ["customer-paper schema observations are missing or malformed"]

    failures: list[str] = []
    failures.extend(
        customer_paper_tenant_policy_failures(observed.get("customer_paper_tenant_policies"))
    )
    failures.extend(
        customer_paper_backend_policy_failures(observed.get("customer_paper_backend_policies"))
    )
    if observed.get("customer_paper_schema_present") is not True:
        failures.append("customer_paper schema is missing")

    missing_tables = observed.get("customer_paper_tables_missing")
    if not isinstance(missing_tables, list):
        failures.append("customer_paper_tables_missing was not reported as a JSON array")
    else:
        for name in missing_tables:
            if not isinstance(name, str) or not name.strip():
                failures.append("customer_paper_tables_missing contains malformed table names")
            else:
                failures.append(f"required customer-paper table missing: {name}")

    integer_checks = (
        ("customer_paper_tables_without_forced_rls", 0),
        ("customer_paper_policy_count", 9),
        ("customer_paper_tenant_policy_count", 7),
        ("customer_paper_backend_policy_count", 2),
        ("customer_paper_wrong_policy_role_count", 0),
        ("customer_paper_runtime_invalid_table_privileges", 0),
        ("customer_paper_runtime_sequences", 4),
        ("customer_paper_runtime_sequences_with_usage", 4),
        ("customer_paper_runtime_sequences_with_forbidden_privileges", 0),
        ("customer_paper_runtime_public_financial_privileges", 0),
    )
    for key, expected in integer_checks:
        actual = observed.get(key)
        if type(actual) is not int:
            failures.append(f"{key} must be a JSON integer, observed {actual!r}")
        elif actual != expected:
            failures.append(f"{key} expected {expected}, observed {actual}")

    boolean_checks = (
        ("customer_paper_runtime_role_valid", True),
        ("customer_paper_runtime_schema_usage", True),
        ("customer_paper_runtime_schema_create", False),
        ("customer_paper_runtime_public_schema_usage", False),
    )
    for key, expected in boolean_checks:
        if observed.get(key) is not expected:
            failures.append(f"{key} expected {expected}, observed {observed.get(key)!r}")
    return failures

RESEARCH_INTEGRITY_QUERY = r"""
SELECT jsonb_build_object(
  'research_trial_ledger_head_rows', (
    SELECT count(id) FROM public.research_trial_ledger_head
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
    target_kind = os.environ.get("RESTORE_TARGET_KIND", "supabase").strip().lower()
    staging_ref = os.environ.get("STAGING_SUPABASE_PROJECT_REF", "")
    production_ref = os.environ.get("PRODUCTION_SUPABASE_PROJECT_REF", "")
    cloud_sql_instance = os.environ.get("CLOUD_SQL_INSTANCE_CONNECTION_NAME", "").strip()
    cloud_sql_database = os.environ.get("CLOUD_SQL_DATABASE", "").strip()
    if target_kind == "supabase" and staging_ref == production_ref:
        raise ValueError("full restore audit requires a distinct non-production target; same-project restore could overwrite the active source")
    if target_kind == "cloud_sql":
        production_instance = os.environ.get("PRODUCTION_CLOUD_SQL_INSTANCE_CONNECTION_NAME", "").strip()
        source_supabase_project_ref = os.environ.get("SOURCE_SUPABASE_PROJECT_REF", "").strip()
        if production_instance:
            # Legacy Cloud SQL-to-Cloud SQL source path remains available only when
            # an explicit production Cloud SQL instance is configured.
            validate_cloud_sql_target_pair(cloud_sql_instance, production_instance)
        else:
            validate_cloud_sql_target_for_supabase_source(
                cloud_sql_instance, source_supabase_project_ref
            )
    database_url = os.environ.get("DATABASE_URL", "")
    env, host = read_database_uri(database_url, staging_ref, production_ref,
        target_kind=target_kind, cloud_sql_instance=cloud_sql_instance, cloud_sql_database=cloud_sql_database)
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
    verified_target_instance = str(args.verified_target_instance_connection_name or "").strip()
    if target_kind == "supabase" and verified_target_ref != staging_ref:
        raise ValueError("Provider restore-operation evidence was not verified against the configured staging project")
    if target_kind == "cloud_sql" and verified_target_instance != cloud_sql_instance:
        raise ValueError("Provider restore-operation evidence was not verified against the configured Cloud SQL instance")
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

    observed = add_research_integrity_observations(observed, env=env)
    if observed.get("current_user") != observed.get("session_user"):
        raise ValueError("audit connection current_user/session_user identity mismatch")
    expected_revision = args.expected_revision
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
    failures.extend(customer_paper_schema_failures(observed))
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
        "verified_restore_target_project_ref": verified_target_ref or None,
        "verified_restore_target_instance_connection_name": verified_target_instance or None,
        "production_supabase_ref": os.environ.get("PRODUCTION_SUPABASE_PROJECT_REF", ""),
        "staging_supabase_ref": staging_ref or None,
        "restore_target_kind": target_kind,
        "cloud_sql_instance_connection_name": cloud_sql_instance or None,
        "cloud_sql_database": cloud_sql_database or None,
        "same_project_staging": (staging_ref == production_ref) if target_kind == "supabase" else None,
        "staging_scope": (
            "cloud_sql_non_production_instance" if target_kind == "cloud_sql"
            else ("same_supabase_project" if staging_ref == production_ref else "separate_supabase_project")
        ),
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
    parser.add_argument("--verified-target-project-ref", default="", help="Supabase target ref verified from provider record")
    parser.add_argument("--verified-target-instance-connection-name", default="", help="Cloud SQL instance verified from provider record")
    parser.add_argument("--expected-audit-role", "--expected-runtime-role", dest="expected_audit_role", required=True)
    parser.add_argument("--expected-revision", default="0065_ledger_trigger_owner_isolation")
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
