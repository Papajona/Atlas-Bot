"""Preflight controls for an isolated customer paper ledger.

No real-money tables, adapters, or customer-fund sessions are imported here. The
paper database URL is never allowed to silently fall back to DATABASE_URL.
Execution remains disabled until a reviewed staging preflight passes.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import uuid
from typing import Any
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from .config import settings


REAL_MONEY_TABLES = (
    "customer_ledger_accounts", "ledger_journals", "ledger_journal_lines",
    "ledger_entries", "withdrawals", "wallets", "funding_transactions", "tron_sweeps",
)
PAPER_TABLES = (
    "customer_paper.accounts", "customer_paper.journals", "customer_paper.journal_lines",
)


def _postgres_url(value: str) -> URL:
    try:
        url = make_url(value)
    except Exception as exc:
        raise ValueError("database URL is malformed") from exc
    if not url.drivername.startswith("postgresql"):
        raise ValueError("customer paper isolation requires PostgreSQL URLs")
    if not url.host or not url.database or not url.username or not url.password:
        raise ValueError("database URL must include host, database, username and password")
    # Async preflight always uses asyncpg; normalize plain postgresql URLs.
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+asyncpg")
    if url.drivername != "postgresql+asyncpg":
        raise ValueError("database URL must use the postgresql+asyncpg driver")
    return url


def validate_database_url_pair(primary_url: str, paper_url: str) -> dict[str, Any]:
    """Validate distinct server-side credentials/endpoints before any network access."""
    primary = _postgres_url(primary_url)
    paper = _postgres_url(paper_url)
    if (primary.host.casefold(), primary.port or 5432, primary.database, primary.username) == (
        paper.host.casefold(), paper.port or 5432, paper.database, paper.username
    ):
        raise ValueError("customer paper ledger must not reuse the primary database identity")
    if primary.username == paper.username:
        raise ValueError("customer paper ledger requires a distinct PostgreSQL runtime role")
    if primary.password == paper.password:
        raise ValueError("customer paper ledger requires a distinct database credential")
    return {
        "primary_host": primary.host.casefold(),
        "paper_host": paper.host.casefold(),
        "same_server": (primary.host.casefold(), primary.port or 5432) == (paper.host.casefold(), paper.port or 5432),
        "primary_database": primary.database,
        "paper_database": paper.database,
        "distinct_runtime_roles": True,
        "distinct_credentials": True,
    }


async def preflight_isolated_customer_paper_ledger(
    *,
    primary_url: str | None = None,
    paper_url: str | None = None,
    require_enabled: bool = True,
) -> dict[str, Any]:
    """Verify DB identities, RLS, paper schema, and no privileges on real-money tables.

    This is a read-only preflight. It does not create the schema, write paper orders,
    enable customer paper trading, or certify the service without a real staging run.
    """
    if require_enabled and not settings.customer_paper_ledger_enabled:
        return {"status": "BLOCKED", "reason": "CUSTOMER_PAPER_LEDGER_FEATURE_DISABLED"}
    primary_value = primary_url if primary_url is not None else settings.database_url
    paper_value = paper_url if paper_url is not None else settings.customer_paper_database_url
    if not paper_value:
        return {"status": "BLOCKED", "reason": "CUSTOMER_PAPER_DATABASE_URL_MISSING"}
    try:
        pair = validate_database_url_pair(primary_value, paper_value)
    except ValueError as exc:
        return {"status": "BLOCKED", "reason": "DATABASE_URL_ISOLATION_INVALID", "detail": str(exc)}

    engine = create_async_engine(
        paper_value if "+asyncpg" in paper_value else _postgres_url(paper_value).render_as_string(hide_password=False),
        pool_pre_ping=True,
        connect_args={"server_settings": {"statement_timeout": "10000"}},
    )
    try:
        async with engine.connect() as conn:
            identity = (await conn.exec_driver_sql(
                "SELECT current_database(), current_user, "
                "current_setting('transaction_read_only'), "
                "(SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication "
                " FROM pg_roles WHERE rolname = current_user), "
                "(SELECT rolinherit OR rolbypassrls FROM pg_roles WHERE rolname = current_user), "
                "(SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member "
                " WHERE r.rolname=current_user), "
                "has_schema_privilege(current_user, 'public', 'USAGE'), "
                "has_schema_privilege(current_user, 'public', 'CREATE')"
            )).one()
            (db_name, role_name, read_only, elevated, unsafe_rls_role, memberships,
             public_schema_usage, public_schema_create) = identity
            if db_name != _postgres_url(paper_value).database:
                return {"status": "BLOCKED", "reason": "PAPER_DATABASE_IDENTITY_MISMATCH"}
            if role_name == _postgres_url(primary_value).username:
                return {"status": "BLOCKED", "reason": "PAPER_ROLE_REUSES_PRIMARY_ROLE"}
            if bool(elevated) or bool(unsafe_rls_role) or int(memberships) != 0:
                return {"status": "BLOCKED", "reason": "PAPER_ROLE_HAS_UNSAFE_DATABASE_CAPABILITIES"}
            if str(read_only).lower() not in {"off", "false", "f"}:
                # A global read-only connection is insufficient to verify order write grants.
                return {"status": "BLOCKED", "reason": "PAPER_CONNECTION_IS_READ_ONLY"}
            if bool(public_schema_usage) or bool(public_schema_create):
                return {"status": "BLOCKED", "reason": "PAPER_ROLE_HAS_UNEXPECTED_PUBLIC_SCHEMA_ACCESS"}
            table_rows = (await conn.execute(text(
                """
                SELECT n.nspname || '.' || c.relname AS table_name,
                       c.relkind, c.relrowsecurity, c.relforcerowsecurity,
                       has_table_privilege(current_user, c.oid, 'SELECT') AS can_select,
                       has_table_privilege(current_user, c.oid, 'INSERT') AS can_insert,
                       has_table_privilege(current_user, c.oid, 'UPDATE') AS can_update,
                       has_table_privilege(current_user, c.oid, 'DELETE') AS can_delete
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE (n.nspname='customer_paper' AND c.relname IN ('accounts','journals','journal_lines'))
                   OR (n.nspname='public' AND c.relname = ANY(:real_tables))
                """),
                {"real_tables": list(REAL_MONEY_TABLES)},
            )).mappings().all()
            by_name = {str(row["table_name"]): row for row in table_rows}
            missing_paper = sorted(set(PAPER_TABLES) - set(by_name))
            if missing_paper:
                return {"status": "BLOCKED", "reason": "PAPER_SCHEMA_NOT_PROVISIONED",
                        "missing_tables": missing_paper, "identity": {"database": db_name, "role": role_name}}
            insecure_paper = []
            for name in PAPER_TABLES:
                row = by_name[name]
                if row["relkind"] not in {"r", "p"}:
                    insecure_paper.append(f"{name}:NOT_A_TABLE")
                if not bool(row["relrowsecurity"]) or not bool(row["relforcerowsecurity"]):
                    insecure_paper.append(f"{name}:RLS_NOT_FORCED")
                if not bool(row["can_select"]) or not bool(row["can_insert"]):
                    insecure_paper.append(f"{name}:SELECT_OR_INSERT_MISSING")
                if bool(row["can_update"]):
                    insecure_paper.append(f"{name}:UPDATE_ALLOWED")
                if bool(row["can_delete"]):
                    insecure_paper.append(f"{name}:DELETE_ALLOWED")
            policy_rows = (await conn.execute(text(
                "SELECT tablename, coalesce(qual,'') AS qual, coalesce(with_check,'') AS with_check, roles "
                "FROM pg_policies WHERE schemaname='customer_paper'"
            ))).mappings().all()
            policy_by_table = {str(row["tablename"]): row for row in policy_rows}
            for table in ("accounts", "journals", "journal_lines"):
                row = policy_by_table.get(table)
                policy_roles = {str(value).casefold() for value in (row["roles"] if row is not None else [])}
                if (row is None or "app.customer_id" not in str(row["qual"])
                        or "app.customer_id" not in str(row["with_check"])
                        or str(role_name).casefold() not in policy_roles):
                    insecure_paper.append(f"customer_paper.{table}:TENANT_POLICY_MISSING")
            if insecure_paper:
                return {"status": "BLOCKED", "reason": "PAPER_SCHEMA_PRIVILEGES_UNSAFE",
                        "findings": insecure_paper}
            exposed_real_tables = []
            for name in REAL_MONEY_TABLES:
                row = by_name.get("public." + name)
                if row is None:
                    continue
                for permission in ("can_select", "can_insert", "can_update", "can_delete"):
                    if bool(row[permission]):
                        exposed_real_tables.append(f"public.{name}:{permission}")
            if exposed_real_tables:
                return {"status": "BLOCKED", "reason": "PAPER_ROLE_HAS_REAL_MONEY_TABLE_PRIVILEGES",
                        "privileges": sorted(exposed_real_tables)}
            return {
                "status": "PASS",
                "reason": "ISOLATED_CUSTOMER_PAPER_DATABASE_PREFLIGHT_PASS",
                "identity": {"database": db_name, "role": role_name},
                "url_pair": pair,
                "paper_tables": sorted(PAPER_TABLES),
                "real_money_table_privileges": "NONE_FOR_ALL_EXISTING_CHECKED_TABLES",
                "read_only_preflight": True,
                "enables_customer_paper_trading": False,
            }
    except Exception as exc:
        # Connectivity/auth failures are never accepted as proof of isolation.
        return {"status": "BLOCKED", "reason": "PAPER_DATABASE_PREFLIGHT_ERROR",
                "detail": type(exc).__name__}
    finally:
        await engine.dispose()
