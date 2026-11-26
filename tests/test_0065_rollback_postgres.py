"""PostgreSQL regression for migration 0065 rollback grant provenance."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest


ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "0064_strategy_outcome_recent_index"
MIGRATION_HEAD = "0068_tron_sweep_txid_casefold_unique"
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


def _url_for_identity(url: str, username: str, password: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or "localhost"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{quote(username)}:{quote(password)}@{host}{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _run_alembic_expect_failure(
    env: dict[str, str], expected_message: str, *args: str
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0, "Alembic unexpectedly accepted an unauthorized downgrade identity"
    assert expected_message in output, output


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
                      AND acl.privilege_type = 'USAGE'
                )
                """,
                role_oid,
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
                      AND acl.privilege_type = 'EXECUTE'
                )
                """,
                VALIDATE_FUNCTION,
                role_oid,
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
                os.environ.get("ATLAS_POSTGRES_URL"),
                os.environ.get("DATABASE_URL"),
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

    # The PostgreSQL workflow applies the current Alembic head before running marked
    # integration tests. Revision 0068 has a reversible TRON index downgrade; revisions
    # 0066/0067 remain intentionally forward-only. Alembic then reaches 0064 so this
    # suite can exercise 0065 rollback semantics without restoring the later security ACLs.
    try:
        current = asyncio.run(
            _get_revision(dsn)
        )
        assert current == MIGRATION_HEAD, (
            f"expected disposable database at {MIGRATION_HEAD}, got {current!r}"
        )
        _run_alembic(env, "downgrade", PREDECESSOR)
        asyncio.run(_run_database_assertions(dsn, "prepare"))

        _run_alembic(env, "upgrade", MIGRATION_HEAD)
        asyncio.run(_run_database_assertions(dsn, "verify-upgrade"))

        _run_alembic(env, "downgrade", PREDECESSOR)
        asyncio.run(_run_database_assertions(dsn, "verify-downgrade"))
    finally:
        # Keep the shared disposable CI database at the repository head even
        # when an assertion fails, so later integration tests see a valid schema.
        _run_alembic(env, "upgrade", MIGRATION_HEAD)


def test_0065_inherited_acl_grantor_without_set_option_is_not_settable():
    dsn = _postgres_test_url()
    nonce = os.urandom(8).hex()
    grantor_role = f"atlas_0065_set_grantor_{nonce}"
    migration_role = f"atlas_0065_set_member_{nonce}"

    async def check() -> None:
        connection = await asyncpg.connect(dsn=dsn)
        try:
            await connection.execute(f"CREATE ROLE {grantor_role} NOLOGIN")
            await connection.execute(f"CREATE ROLE {migration_role} NOLOGIN")
            await connection.execute(
                f"GRANT {grantor_role} TO {migration_role} "
                "WITH INHERIT TRUE, SET FALSE"
            )
            member = await connection.fetchval(
                "SELECT pg_has_role($1, $2, 'MEMBER')",
                migration_role,
                grantor_role,
            )
            can_set = await connection.fetchval(
                "SELECT pg_has_role($1, $2, 'SET')",
                migration_role,
                grantor_role,
            )
            assert member is True
            assert can_set is False

            migration = (
                ROOT
                / "alembic"
                / "versions"
                / "0065_ledger_trigger_owner_isolation.py"
            ).read_text(encoding="utf-8")
            assert "pg_has_role(migration_role_oid, grantor_oid, 'SET')" in migration
            assert "pg_has_role(migration_role_oid, grantor_oid, 'MEMBER')" not in migration
        finally:
            await connection.execute(f"DROP ROLE IF EXISTS {migration_role}")
            await connection.execute(f"DROP ROLE IF EXISTS {grantor_role}")
            await connection.close()

    asyncio.run(check())


def test_0065_reused_trigger_role_keeps_preexisting_direct_acl_grants():
    dsn = _postgres_test_url()
    env = os.environ.copy()
    alembic_url = next(
        (
            value
            for value in (
                os.environ.get("ATLAS_POSTGRES_URL"),
                os.environ.get("DATABASE_URL"),
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

    async def prepare_and_check(stage: str) -> None:
        connection = await asyncpg.connect(dsn=dsn)
        try:
            if stage == "prepare":
                revision = await connection.fetchval(
                    "SELECT version_num FROM public.alembic_version"
                )
                assert revision == PREDECESSOR, revision
                exists = await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)",
                    TRIGGER_OWNER,
                )
                assert not exists
                await connection.execute(
                    "CREATE ROLE atlas_ledger_trigger_owner "
                    "NOLOGIN NOINHERIT NOBYPASSRLS NOSUPERUSER "
                    "NOCREATEDB NOCREATEROLE NOREPLICATION"
                )
                await connection.execute(
                    "COMMENT ON ROLE atlas_ledger_trigger_owner IS 'pre-existing-trigger-owner-comment'"
                )
                # A separate grantor creates the direct ACL entries. This
                # reproduces a reused trigger role whose privileges predate
                # 0065 and were not granted by the migration identity.
                await connection.execute(
                    "CREATE ROLE atlas_0065_acl_owner NOLOGIN NOINHERIT"
                )
                await connection.execute(
                    "GRANT USAGE ON SCHEMA public TO atlas_0065_acl_owner WITH GRANT OPTION"
                )
                await connection.execute(
                    "GRANT SELECT ON TABLE public.ledger_journals, "
                    "public.ledger_journal_lines TO atlas_0065_acl_owner WITH GRANT OPTION"
                )
                await connection.execute(
                    f"GRANT EXECUTE ON FUNCTION {VALIDATE_FUNCTION} "
                    "TO atlas_0065_acl_owner WITH GRANT OPTION"
                )
                await connection.execute("SET ROLE atlas_0065_acl_owner")
                await connection.execute(
                    "GRANT USAGE ON SCHEMA public TO atlas_ledger_trigger_owner"
                )
                await connection.execute(
                    "GRANT SELECT ON TABLE public.ledger_journals, "
                    "public.ledger_journal_lines TO atlas_ledger_trigger_owner"
                )
                await connection.execute(
                    f"GRANT EXECUTE ON FUNCTION {VALIDATE_FUNCTION} "
                    "TO atlas_ledger_trigger_owner"
                )
                await connection.execute("RESET ROLE")

            elif stage in {"verify-upgrade", "verify-downgrade"}:
                expected_revision = (
                    MIGRATION_HEAD if stage == "verify-upgrade" else PREDECESSOR
                )
                revision = await connection.fetchval(
                    "SELECT version_num FROM public.alembic_version"
                )
                assert revision == expected_revision, revision
                role_oid = await connection.fetchval(
                    "SELECT oid FROM pg_roles WHERE rolname = $1",
                    TRIGGER_OWNER,
                )
                assert role_oid is not None, "0065 discarded a role that predated the migration"

                # Each existing ACL must still be owned by the original
                # grantor, with no duplicate grant attributed to migration_role.
                schema_direct = await connection.fetchval(
                    """
                    SELECT count(*) = 1 AND bool_and(
                        acl.grantor = (
                            SELECT oid FROM pg_roles WHERE rolname = $2
                        )
                    )
                    FROM pg_namespace n
                    CROSS JOIN LATERAL aclexplode(
                        coalesce(n.nspacl, acldefault('n', n.nspowner))
                    ) acl
                    WHERE n.nspname = 'public'
                      AND acl.grantee = $1
                      AND acl.privilege_type = 'USAGE'
                    """,
                    role_oid,
                    "atlas_0065_acl_owner",
                )
                journals_direct = await connection.fetchval(
                    """
                    SELECT count(*) = 1 AND bool_and(
                        acl.grantor = (
                            SELECT oid FROM pg_roles WHERE rolname = $2
                        )
                    )
                    FROM pg_class c
                    CROSS JOIN LATERAL aclexplode(
                        coalesce(c.relacl, acldefault('r', c.relowner))
                    ) acl
                    WHERE c.oid = to_regclass('public.ledger_journals')
                      AND acl.grantee = $1
                      AND acl.privilege_type = 'SELECT'
                    """,
                    role_oid,
                    "atlas_0065_acl_owner",
                )
                lines_direct = await connection.fetchval(
                    """
                    SELECT count(*) = 1 AND bool_and(
                        acl.grantor = (
                            SELECT oid FROM pg_roles WHERE rolname = $2
                        )
                    )
                    FROM pg_class c
                    CROSS JOIN LATERAL aclexplode(
                        coalesce(c.relacl, acldefault('r', c.relowner))
                    ) acl
                    WHERE c.oid = to_regclass('public.ledger_journal_lines')
                      AND acl.grantee = $1
                      AND acl.privilege_type = 'SELECT'
                    """,
                    role_oid,
                    "atlas_0065_acl_owner",
                )
                function_direct = await connection.fetchval(
                    """
                    SELECT count(*) = 1 AND bool_and(
                        acl.grantor = (
                            SELECT oid FROM pg_roles WHERE rolname = $3
                        )
                    )
                    FROM pg_proc p
                    CROSS JOIN LATERAL aclexplode(
                        coalesce(p.proacl, acldefault('f', p.proowner))
                    ) acl
                    WHERE p.oid = to_regprocedure($1)
                      AND acl.grantee = $2
                      AND acl.privilege_type = 'EXECUTE'
                    """,
                    VALIDATE_FUNCTION,
                    role_oid,
                    "atlas_0065_acl_owner",
                )
                assert schema_direct
                assert journals_direct
                assert lines_direct
                assert function_direct

                if stage == "verify-downgrade":
                    # A downgrade must put function ownership back on the
                    # migration identity so older revisions can continue with
                    # owner-only DDL (including their subsequent DROP FUNCTION).
                    migration_identity = await connection.fetchval(
                        "SELECT current_user"
                    )
                    restored_function_owner = await connection.fetchval(
                        """
                        SELECT owner.rolname
                        FROM pg_proc function_row
                        JOIN pg_roles owner ON owner.oid = function_row.proowner
                        WHERE function_row.oid = to_regprocedure($1)
                        """,
                        "public.atlas_ledger_journal_deferred_check()",
                    )
                    assert restored_function_owner == migration_identity, (
                        "0065 downgrade did not restore the deferred trigger "
                        "function to the original migration identity"
                    )
                    comment = await connection.fetchval(
                        "SELECT shobj_description(oid, 'pg_authid') "
                        "FROM pg_roles WHERE rolname = $1",
                        TRIGGER_OWNER,
                    )
                    assert comment == "pre-existing-trigger-owner-comment"
            else:
                raise AssertionError(stage)
        finally:
            await connection.close()

    try:
        current = asyncio.run(_get_revision(dsn))
        assert current == MIGRATION_HEAD, current
        _run_alembic(env, "downgrade", PREDECESSOR)
        asyncio.run(prepare_and_check("prepare"))

        _run_alembic(env, "upgrade", MIGRATION_HEAD)
        asyncio.run(prepare_and_check("verify-upgrade"))

        _run_alembic(env, "downgrade", PREDECESSOR)
        asyncio.run(prepare_and_check("verify-downgrade"))
    finally:
        async def cleanup() -> None:
            revision = await _get_revision(dsn)
            if revision == MIGRATION_HEAD:
                _run_alembic(env, "downgrade", PREDECESSOR)
            connection = await asyncpg.connect(dsn=dsn)
            try:
                grantor_exists = await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_roles "
                    "WHERE rolname = 'atlas_0065_acl_owner')"
                )
                if grantor_exists:
                    # These pre-existing grants were issued by the separate
                    # grantor role. Revoke them under that grantor, not under
                    # the migration identity, before dropping the reused role.
                    await connection.execute("SET ROLE atlas_0065_acl_owner")
                    await connection.execute(
                        "REVOKE EXECUTE ON FUNCTION "
                        "public.atlas_validate_ledger_journal(integer) "
                        "FROM atlas_ledger_trigger_owner"
                    )
                    await connection.execute(
                        "REVOKE SELECT ON TABLE public.ledger_journals, "
                        "public.ledger_journal_lines FROM atlas_ledger_trigger_owner"
                    )
                    await connection.execute(
                        "REVOKE USAGE ON SCHEMA public FROM atlas_ledger_trigger_owner"
                    )
                    await connection.execute("RESET ROLE")
                await connection.execute(
                    "DROP ROLE IF EXISTS atlas_ledger_trigger_owner"
                )
                if grantor_exists:
                    await connection.execute(
                        "REVOKE USAGE ON SCHEMA public FROM atlas_0065_acl_owner"
                    )
                    await connection.execute(
                        "REVOKE SELECT ON TABLE public.ledger_journals, "
                        "public.ledger_journal_lines FROM atlas_0065_acl_owner"
                    )
                    await connection.execute(
                        f"REVOKE EXECUTE ON FUNCTION {VALIDATE_FUNCTION} "
                        "FROM atlas_0065_acl_owner"
                    )
                    await connection.execute("DROP ROLE IF EXISTS atlas_0065_acl_owner")
            finally:
                await connection.close()

        try:
            asyncio.run(cleanup())
        finally:
            _run_alembic(env, "upgrade", MIGRATION_HEAD)


def test_0065_downgrade_rejects_a_different_administrator_before_mutating():
    dsn = _postgres_test_url()
    env = os.environ.copy()
    alembic_url = next(
        (
            value
            for value in (
                os.environ.get("ATLAS_POSTGRES_URL"),
                os.environ.get("DATABASE_URL"),
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

    alternate_user = "atlas_0065_downgrade_alt"
    role_nonce = os.urandom(16).hex()

    async def create_alternate_admin() -> None:
        connection = await asyncpg.connect(dsn=dsn)
        try:
            await connection.execute(f"DROP ROLE IF EXISTS {alternate_user}")
            await connection.execute(
                f"CREATE ROLE {alternate_user} LOGIN SUPERUSER "
                f"PASSWORD '{role_nonce}'"
            )
        finally:
            await connection.close()

    async def verify_unchanged() -> None:
        connection = await asyncpg.connect(dsn=dsn)
        try:
            revision = await connection.fetchval(
                "SELECT version_num FROM public.alembic_version"
            )
            assert revision == MIGRATION_HEAD, revision
            owner = await connection.fetchval(
                """
                SELECT r.rolname
                FROM pg_proc p
                JOIN pg_roles r ON r.oid = p.proowner
                WHERE p.oid = to_regprocedure(
                    'public.atlas_ledger_journal_deferred_check()'
                )
                """
            )
            assert owner == TRIGGER_OWNER, owner
            marker = await connection.fetchval(
                "SELECT shobj_description(oid, 'pg_authid') "
                "FROM pg_roles WHERE rolname = $1",
                TRIGGER_OWNER,
            )
            assert marker and marker.startswith("atlas:0065:rollback-state:v3:")
        finally:
            await connection.close()

    async def drop_alternate_admin() -> None:
        connection = await asyncpg.connect(dsn=dsn)
        try:
            await connection.execute(f"DROP ROLE IF EXISTS {alternate_user}")
        finally:
            await connection.close()

    try:
        current = asyncio.run(_get_revision(dsn))
        assert current == MIGRATION_HEAD, current
        asyncio.run(create_alternate_admin())
        alternate_env = env.copy()
        alternate_env["DATABASE_URL"] = _url_for_identity(
            alembic_url, alternate_user, role_nonce
        )
        _run_alembic_expect_failure(
            alternate_env,
            "run Alembic as the original migration identity",
            "downgrade",
            PREDECESSOR,
        )
        asyncio.run(verify_unchanged())
        asyncio.run(drop_alternate_admin())
        _run_alembic(env, "downgrade", PREDECESSOR)
        assert asyncio.run(_get_revision(dsn)) == PREDECESSOR
    finally:
        try:
            asyncio.run(drop_alternate_admin())
        finally:
            _run_alembic(env, "upgrade", MIGRATION_HEAD)


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

def test_0066_research_trigger_acl_revokes_direct_supabase_api_grants():
    """0066 must remove both PUBLIC and historical direct API-role execution grants."""
    dsn = _postgres_test_url()
    env = os.environ.copy()
    alembic_url = next(
        (
            value
            for value in (
                os.environ.get("ATLAS_POSTGRES_URL"),
                os.environ.get("DATABASE_URL"),
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

    api_roles = ("anon", "authenticated")
    trigger_functions = (
        "public.atlas_research_trial_archive_immutable()",
        "public.atlas_research_holdout_registry_immutable()",
    )
    created_roles: list[str] = []
    granted_privileges: list[tuple[str, str]] = []

    async def grant_direct_execution() -> None:
        connection = await asyncpg.connect(dsn=dsn)
        try:
            for role_name in api_roles:
                exists = await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)",
                    role_name,
                )
                if not exists:
                    await connection.execute(f"CREATE ROLE {role_name} NOLOGIN")
                    created_roles.append(role_name)
                for signature in trigger_functions:
                    already_can_execute = await connection.fetchval(
                        """
                        SELECT has_function_privilege(
                            $1::name,
                            to_regprocedure($2)::oid,
                            'EXECUTE'
                        )
                        """,
                        role_name,
                        signature,
                    )
                    assert already_can_execute is False, (
                        f"0065 baseline unexpectedly grants {role_name} EXECUTE "
                        f"on {signature}; refusing to overwrite pre-existing access"
                    )
                    await connection.execute(
                        f"GRANT EXECUTE ON FUNCTION {signature} TO {role_name}"
                    )
                    granted_privileges.append((role_name, signature))
        finally:
            await connection.close()

    async def assert_execution_revoked() -> None:
        connection = await asyncpg.connect(dsn=dsn)
        try:
            revision = await connection.fetchval(
                "SELECT version_num FROM public.alembic_version"
            )
            assert revision == "0066_pin_research_trigger_path", revision
            for role_name in api_roles:
                for signature in trigger_functions:
                    can_execute = await connection.fetchval(
                        """
                        SELECT has_function_privilege(
                            $1::name,
                            to_regprocedure($2)::oid,
                            'EXECUTE'
                        )
                        """,
                        role_name,
                        signature,
                    )
                    assert can_execute is False, (
                        f"0066 left {role_name} able to EXECUTE {signature}"
                    )
        finally:
            await connection.close()

    async def cleanup_created_roles() -> None:
        connection = await asyncpg.connect(dsn=dsn)
        try:
            # Revoke only privileges this test actually introduced. In
            # particular, never remove an operator's pre-existing API-role ACL.
            for role_name, signature in granted_privileges:
                exists = await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)",
                    role_name,
                )
                if exists:
                    await connection.execute(
                        f"REVOKE EXECUTE ON FUNCTION {signature} FROM {role_name}"
                    )

            for role_name in created_roles:
                exists = await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)",
                    role_name,
                )
                if exists:
                    await connection.execute(f"DROP ROLE {role_name}")
        finally:
            await connection.close()

    try:
        current = asyncio.run(_get_revision(dsn))
        assert current == MIGRATION_HEAD, (
            f"expected disposable database at {MIGRATION_HEAD}, got {current!r}"
        )
        # Stop after 0065 because that migration already revokes the API-role
        # grants. Recreate the direct grants only after 0065 has completed so
        # this regression proves that 0066 itself removes them.
        _run_alembic(env, "downgrade", "0065_ledger_trigger_owner_isolation")
        asyncio.run(grant_direct_execution())

        # Upgrade only through 0066 so no later migration can mask a
        # missing direct-grant revoke in the revision under test.
        _run_alembic(env, "upgrade", "0066_pin_research_trigger_path")
        asyncio.run(assert_execution_revoked())
    finally:
        try:
            # Keep the shared disposable PostgreSQL database at the exact head.
            _run_alembic(env, "upgrade", MIGRATION_HEAD)
        finally:
            asyncio.run(cleanup_created_roles())

