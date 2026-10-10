from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import text

from .config import settings
from .db import engine
from .rls_role_policy import require_rls_bypass_role

def assert_multidict_safe_backend() -> None:
    """Refuse production/staging startup unless multidict uses the safe Python backend."""
    environment = str(settings.environment).lower()
    if environment not in {"production", "staging"}:
        return
    if os.environ.get("MULTIDICT_NO_EXTENSIONS") != "1":
        raise RuntimeError("MULTIDICT_NO_EXTENSIONS=1 is required outside development")
    try:
        import multidict
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
                "SELECT current_user AS role_name, r.rolsuper AS is_superuser, "
                "r.rolbypassrls AS bypass_rls, NOT EXISTS ("
                "SELECT 1 FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
                "AND c.relrowsecurity AND (c.relowner <> r.oid OR c.relforcerowsecurity)"
                ") AS owns_all_rls_tables FROM pg_roles AS r WHERE r.rolname = current_user"
            ))
            row = result.mappings().one_or_none()
        if row is None:
            raise RuntimeError("Unable to identify the current PostgreSQL application role")
        require_rls_bypass_role(
            str(row["role_name"]), bool(row["is_superuser"]), bool(row["bypass_rls"]),
            bool(row["owns_all_rls_tables"])
        )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"Unable to verify database role compatibility with RLS lockdown: {exc}"
        ) from exc

