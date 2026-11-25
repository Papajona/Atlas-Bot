"""PostgreSQL regression for migration 0065 rollback grant provenance."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import pytest


ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "0064_strategy_outcome_recent_index"
MIGRATION_HEAD = "0065_ledger_trigger_owner_isolation"
TRIGGER_OWNER = "atlas_ledger_trigger_owner"
VALIDATE_FUNCTION = "public.atlas_validate_ledger_journal(integer)"

pytestmark = pytest.mark.postgres


def _postgres_test_url() -> str:
    value = os.environ.get("ATLAS_POSTGRES_URL") or os.environ.get("DATABASE_URL")
    if not value:
        pytest.skip("requires the mandatory disposable PostgreSQL service")
    if value.startswith("postgresql+asyncpg://"):
        value = "postgresql://" + value.removeprefix("postgresql+asyncpg://")
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        pytest.skip("requires a PostgreSQL URL")
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        pytest.fail("0065 rollback regression refuses to mutate a non-local database")
    return value


def _run_alembic(env: dict[str, str], *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"alembic {' '.join(args)} failed with exit {result.returncode}:\n"
            f"{result.stdout}\n{result.stderr}"
        )


async def _run_database_assertions(dsn: str, stage: str) -> None:
    connection = await asyncpg.connect(dsn=dsn)
    try:
        if stage == "prepare":
            revision = await connection.fetchval(
                "SELECT version_num FROM public.alembic_version"
            )
            assert revision == PREDECESSOR, revision
            role_exists = await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)",
                TRIGGER_OWNER,
            )
            assert not role_exists, "0065 trigger owner remained after the baseline downgrade"

            # Reproduce the Qodo case: the trigger role has no direct grants
            # before 0065, while PUBLIC already supplies schema USAGE and
            # validation-function EXECUTE to every role.
            await connection.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")
            await connection.execute(
                f"GRANT EXECUTE ON FUNCTION {VALIDATE_FUNCTION} TO PUBLIC"
            )
            public_schema_usage = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_namespace n
                    CROSS JOIN LATERAL aclexplode(
                        coalesce(n.nspacl, acldefault('n', n.nspowner))
                    ) acl
                    WHERE n.nspname = 'public'
                      AND acl.grantee = 0
                      AND acl.privilege_type = 'USAGE'
                )
                """
            )
            public_function_execute = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_proc p
                    CROSS JOIN LATERAL aclexplode(
                        coalesce(p.proacl, acldefault('f', p.proowner))
                    ) acl
                    WHERE p.oid = to_regprocedure($1)
                      AND acl.grantee = 0
                      AND acl.privilege_type = 'EXECUTE'
                )
                """,
                VALIDATE_FUNCTION,
            )
            assert public_schema_usage
            assert public_function_execute

        elif stage == "verify-upgrade":
            revision = await connection.fetchval(
                "SELECT version_num FROM public.alembic_version"
            )
            assert revision == MIGRATION_HEAD, revision
            role_oid = await connection.fetchval(
                "SELECT oid FROM pg_roles WHERE rolname = $1",
                TRIGGER_OWNER,
            )
            assert role_oid is not None, "0065 did not create its isolated trigger role"
            grantor_oid = await connection.fetchval(
                "SELECT oid FROM pg_roles WHERE rolname = current_user"
            )
            schema_direct = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_namespace n
                    CROSS JOIN LATERAL aclexplode(
                        coalesce(n.nspacl, acldefault('n', n.nspowner))
                    ) acl
                    WHERE n.nspname = 'public'
                      AND acl.grantee = $1
                      AND acl.grantor = $2
                      AND acl.privilege_type = 'USAGE'
                )
                """,
                role_oid,
                grantor_oid,
            )
            function_direct = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_proc p
                    CROSS JOIN LATERAL aclexplode(
                        coalesce(p.proacl, acldefault('f', p.proowner))
                    ) acl
                    WHERE p.oid = to_regprocedure($1)
                      AND acl.grantee = $2
                      AND acl.grantor = $3
                      AND acl.privilege_type = 'EXECUTE'
                )
                """,
                VALIDATE_FUNCTION,
                role_oid,
                grantor_oid,
            )
            assert schema_direct, "0065 did not add its direct schema USAGE grant"
            assert function_direct, "0065 did not add its direct validation EXECUTE grant"

        elif stage == "verify-downgrade":
            revision = await connection.fetchval(
                "SELECT version_num FROM public.alembic_version"
            )
            assert revision == PREDECESSOR, revision
            role_exists = await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)",
                TRIGGER_OWNER,
            )
            assert not role_exists, (
                "0065 downgrade retained a revision-created role even though "
                "its only schema/function access had been supplied by PUBLIC before upgrade"
            )
            public_schema_usage = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_namespace n
                    CROSS JOIN LATERAL aclexplode(
                        coalesce(n.nspacl, acldefault('n', n.nspowner))
                    ) acl
                    WHERE n.nspname = 'public'
                      AND acl.grantee = 0
                      AND acl.privilege_type = 'USAGE'
                )
                """
            )
            assert public_schema_usage, "downgrade unexpectedly removed PUBLIC schema USAGE"
        else:
            raise AssertionError(f"unknown test stage: {stage}")
    finally:
        await connection.close()


def test_0065_downgrade_removes_direct_grants_when_effective_access_came_from_public():
    dsn = _postgres_test_url()
    env = os.environ.copy()
    alembic_url = next(
        (
            value
            for value in (
                os.environ.get("DATABASE_URL"),
                os.environ.get("ATLAS_POSTGRES_URL"),
            )
            if value
            and value.startswith(
                ("postgres://", "postgresql://", "postgresql+asyncpg://")
            )
        ),
        None,
    )
    if alembic_url is None:
        pytest.skip("requires a PostgreSQL DATABASE_URL for Alembic")
    env["DATABASE_URL"] = alembic_url

    # The PostgreSQL workflow applies head before running marked integration
    # tests. Downgrade once to establish an explicit PUBLIC-only baseline.
    try:
        current = asyncio.run(
            _get_revision(dsn)
        )
        assert current == MIGRATION_HEAD, (
            f"expected disposable database at {MIGRATION_HEAD}, got {current!r}"
        )
        _run_alembic(env, "downgrade", PREDECESSOR)
        asyncio.run(_run_database_assertions(dsn, "prepare"))

        _run_alembic(env, "upgrade", "head")
        asyncio.run(_run_database_assertions(dsn, "verify-upgrade"))

        _run_alembic(env, "downgrade", PREDECESSOR)
        asyncio.run(_run_database_assertions(dsn, "verify-downgrade"))
    finally:
        # Keep the shared disposable CI database at the repository head even
        # when an assertion fails, so later integration tests see a valid schema.
        _run_alembic(env, "upgrade", "head")


async def _get_revision(dsn: str) -> str | None:
    connection = await asyncpg.connect(dsn=dsn)
    try:
        exists = await connection.fetchval(
            "SELECT to_regclass('public.alembic_version') IS NOT NULL"
        )
        if not exists:
            return None
        return await connection.fetchval(
            "SELECT version_num FROM public.alembic_version"
        )
    finally:
        await connection.close()
