"""Audit hash-chain regression tests."""
import asyncio
import os
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.audit_chain import append_audit
from app.config import settings
from app.db import AuditChainState, AuditLog, Base

@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")

def test_append_audit_refreshes_state_loaded_before_the_lock(tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'a.db'}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        S = async_sessionmaker(engine, expire_on_commit=False)
        a, b = S(), S()
        try:
            await append_audit(a, event="E1", detail={}, actor_id="t")
            held = await a.get(AuditChainState, 1)
            second = await append_audit(b, event="E2", detail={}, actor_id="t")
            third = await append_audit(a, event="E3", detail={}, actor_id="t")
            assert third.previous_hash == second.event_hash
            rows = (await S().execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
            assert all(y.previous_hash == x.event_hash for x, y in zip(rows, rows[1:]))
            assert held is not None
        finally:
            await a.close()
            await b.close()
            await engine.dispose()
    asyncio.run(go())

@pytest.mark.skipif(not os.getenv("ATLAS_POSTGRES_URL"), reason="needs migrated PostgreSQL (ATLAS_POSTGRES_URL)")
def test_concurrent_audit_writers_never_fork_the_chain_on_postgres():
    from app.audit_chain import record_audit
    async def _all():
        from app.db import SessionLocal
        async with SessionLocal() as db:
            return (await db.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
    async def go():
        rows_before = await _all()
        res = await asyncio.gather(*[record_audit("PG_PROBE", {"i": i}, "t") for i in range(25)], return_exceptions=True)
        assert not [r for r in res if isinstance(r, Exception)]
        added = (await _all())[len(rows_before):]
        assert len(added) == 25
        assert all(y.previous_hash == x.event_hash for x, y in zip(added, added[1:])), "audit chain forked"
    asyncio.run(go())
