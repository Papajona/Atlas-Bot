from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import text

from .config import settings
from .db import engine
from .rls_role_policy import require_public_schema_rls_complete, require_rls_bypass_role

def assert_multidict_safe_backend() -> None:
    """Refuse production/staging startup unless multidict uses the safe Python backend."""
    environment = str(settings.environment).lower()
    if environment not in {"production", "staging"}:
        return
    if os.environ.get("MULTIDICT_NO_EXTENSIONS") != "1":
        raise RuntimeError("MULTIDICT_NO_EXTENSIONS=1 is required outside development")
    try:
        import multidict
        version = str(getattr(multidict, "__version__", ""))
        try:
            version_parts = tuple(int(part) for part in version.split(".")[:3])
        except ValueError as exc:
            raise RuntimeError("Unable to verify multidict version") from exc
        if len(version_parts) != 3 or version_parts < (6, 9, 1):
            raise RuntimeError("multidict>=6.9.1 is required; version is unsupported")
        if str(multidict.CIMultiDict.__module__) != "multidict._multidict_py":
            raise RuntimeError("multidict is not using the required pure-Python backend")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Unable to verify multidict safety backend: {exc}") from exc


async def assert_database_migrations_current() -> None:
    """Fail closed in staging/production when the database is not at Alembic head."""
    if str(settings.environment).lower() == "development":
        return
    try:
        from alembic.config import Config as AlembicConfig
        from alembic.migration import MigrationContext
        from alembic.script import ScriptDirectory
        cfg = AlembicConfig(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        expected = set(ScriptDirectory.from_config(cfg).get_heads())
        async with engine.connect() as conn:
            current = await conn.run_sync(lambda sync_conn: set(MigrationContext.configure(sync_conn).get_current_heads()))
        if current != expected:
            raise RuntimeError(f"Database migrations are not at Alembic head: current={sorted(current)} expected={sorted(expected)}; run 'alembic upgrade head' before starting Atlas")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Unable to verify Alembic migration state: {exc}") from exc

async def assert_database_role_compatible_with_rls_lockdown() -> None:
    """Fail closed in staging/production unless the app DB role can operate behind RLS."""
    if str(settings.environment).lower() not in {"production", "staging"}:
        return
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(
                "SELECT current_user AS role_name, session_user AS login_role, r.rolsuper AS is_superuser, "
                "r.rolbypassrls AS bypass_rls, r.rolcreaterole AS can_create_roles, "
                "r.rolcreatedb AS can_create_databases, r.rolreplication AS can_replicate, "
                "(SELECT count(*) FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')) AS public_table_count, "
                "(SELECT count(*) FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relrowsecurity) "
                " AS tables_without_rls, "
                "(NOT has_schema_privilege('anon', 'public', 'USAGE') "
                " AND NOT has_schema_privilege('authenticated', 'public', 'USAGE')) "
                " AS api_schema_access_blocked, "
                "(SELECT count(*) FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND c.relowner = r.oid) "
                " AS owned_public_table_count, "
                "(SELECT count(*) FROM pg_auth_members AS membership WHERE membership.member = r.oid) "
                "AS role_membership_count FROM pg_roles AS r WHERE r.rolname = current_user"
            ))
            row = result.mappings().one_or_none()
        if row is None:
            raise RuntimeError("Unable to identify the current PostgreSQL application role")
        if str(row["login_role"]) != str(row["role_name"]):
            raise RuntimeError("Database login role differs from current role; SET ROLE is not allowed for Atlas runtime")
        if not bool(row["api_schema_access_blocked"]):
            raise RuntimeError(
                "Public schema is still accessible to anon/authenticated; "
                "the public-schema lockdown must revoke schema USAGE before Atlas starts."
            )
        require_public_schema_rls_complete(
            int(row["public_table_count"]), int(row["tables_without_rls"])
        )
        require_rls_bypass_role(
            str(row["role_name"]),
            bool(row["is_superuser"]),
            bool(row["bypass_rls"]),
            owned_public_table_count=int(row["owned_public_table_count"]),
            can_create_roles=bool(row["can_create_roles"]),
            can_create_databases=bool(row["can_create_databases"]),
            can_replicate=bool(row["can_replicate"]),
            role_membership_count=int(row["role_membership_count"]),
        )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"Unable to verify database role compatibility with RLS lockdown: {exc}"
        ) from exc

