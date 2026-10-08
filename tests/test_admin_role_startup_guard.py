import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db as appdb
import app.startup_guards as guards
from app.config import settings
from app.db import AdminRole, Base


def _setup(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin.db'}")

    async def init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(init())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(appdb, "SessionLocal", maker)
    monkeypatch.setattr(guards, "SessionLocal", maker, raising=False)
    return engine, maker


def _run_guard(tmp_path, monkeypatch, *, assignments="", rows=(), environment="production", process_role="api"):
    engine, maker = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "environment", environment, raising=False)
    monkeypatch.setattr(settings, "process_role", process_role, raising=False)
    monkeypatch.setattr(settings, "admin_supabase_user_ids", "u-admin", raising=False)
    monkeypatch.setattr(settings, "admin_role_assignments", assignments, raising=False)

    async def run():
        async with maker() as db:
            for uid, role, active in rows:
                db.add(AdminRole(auth_user_id=uid, role=role, active=active))
            await db.commit()
        await guards.assert_admin_roles_provisioned()
        await engine.dispose()

    asyncio.run(run())


def test_admin_guard_blocks_without_effective_administrator(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="No ADMINISTRATOR is provisioned"):
        _run_guard(tmp_path, monkeypatch)


def test_admin_guard_accepts_allowlisted_environment_administrator(tmp_path, monkeypatch):
    _run_guard(tmp_path, monkeypatch, assignments="u-admin:ADMINISTRATOR")


def test_admin_guard_does_not_resurrect_database_demoted_admin(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="No ADMINISTRATOR is provisioned"):
        _run_guard(
            tmp_path,
            monkeypatch,
            assignments="u-admin:ADMINISTRATOR",
            rows=[("u-admin", "ADMINISTRATOR", False)],
        )


def test_admin_guard_accepts_active_database_administrator(tmp_path, monkeypatch):
    _run_guard(tmp_path, monkeypatch, rows=[("u-db", "ADMINISTRATOR", True)])


def test_admin_guard_is_inactive_for_workers(tmp_path, monkeypatch):
    _run_guard(tmp_path, monkeypatch, process_role="worker")


def test_production_totp_and_live_request_ttl_are_fail_closed():
    from pydantic import ValidationError
    from app.config import Settings

    assert Settings.model_fields["live_enable_request_ttl_minutes"].default == 60
    with pytest.raises(ValidationError, match="ADMIN_TOTP_REQUIRED must be true in production"):
        Settings(environment="production", admin_totp_required=False)


def test_live_enable_expiry_guard_is_present():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert "live-trading enablement request expired" in src
