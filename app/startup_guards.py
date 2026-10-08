from __future__ import annotations

import os
from pathlib import Path

from .config import settings
from .db import engine

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


async def assert_admin_roles_provisioned() -> None:
    """Fail closed in staging/production API processes when no effective ADMINISTRATOR exists."""
    if str(settings.environment).lower() not in {"production", "staging"} or str(settings.process_role) != "api":
        return
    from sqlalchemy import select
    from .admin_rbac import _env_assignments
    from .db import AdminRole, SessionLocal
    allowed = {x.strip() for x in str(settings.admin_supabase_user_ids or "").split(",") if x.strip()}
    env_admins = {uid for uid, role in _env_assignments().items() if role == "ADMINISTRATOR" and uid in allowed}
    async with SessionLocal() as db:
        rows = (await db.execute(select(AdminRole.auth_user_id, AdminRole.role, AdminRole.active))).all()
    db_users = {str(r[0]) for r in rows}
    db_admins = {str(r[0]) for r in rows if bool(r[2]) and str(r[1]).upper() == "ADMINISTRATOR"}
    effective = db_admins | {u for u in env_admins if u not in db_users}
    if not effective:
        raise RuntimeError("No ADMINISTRATOR is provisioned: set ADMIN_ROLE_ASSIGNMENTS for an allow-listed user or grant the role in the database before starting the API in staging/production")
