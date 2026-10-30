"""Audit hash-chain regression tests."""
import asyncio
import os
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.audit_chain import append_audit, append_audit_once
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
            async with S() as reader:
                rows = (await reader.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
                assert all(y.previous_hash == x.event_hash for x, y in zip(rows, rows[1:]))
            assert held is not None
        finally:
            await a.close()
            await b.close()
            await engine.dispose()
    asyncio.run(go())

def test_append_audit_once_recovers_missing_submission_event_without_duplicates(tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'audit-once.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        S = async_sessionmaker(engine, expire_on_commit=False)
        detail = {
            "customer_id": 99231,
            "client_order_id": "cp-stable-order-identity",
            "side": "BUY",
            "quantity": "0.25",
        }
        try:
            async with S() as first_db:
                first, created = await append_audit_once(
                    first_db,
                    event="CUSTOMER_PAPER_ORDER_SUBMITTED",
                    detail=detail,
                    actor_id="customer:99231",
                    idempotency_fields=("customer_id", "client_order_id"),
                )
            # Corrupt only the encrypted detail payload. Retry de-duplication must
            # use the indexed identity columns and must not deserialize this row.
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE audit_log SET detail='damaged-ciphertext' WHERE id=:id"),
                    {"id": first.id},
                )
            async with S() as recovery_db:
                replay, created_again = await append_audit_once(
                    recovery_db,
                    event="CUSTOMER_PAPER_ORDER_SUBMITTED",
                    detail=detail,
                    actor_id="customer:99231",
                    idempotency_fields=("customer_id", "client_order_id"),
                )
            async with S() as reader:
                rows = (
                    await reader.execute(
                        select(AuditLog.id).where(AuditLog.event == "CUSTOMER_PAPER_ORDER_SUBMITTED")
                    )
                ).scalars().all()
            assert created is True
            assert created_again is False
            assert replay.id == first.id
            assert len(rows) == 1
            assert "uq_audit_log_event_idempotency" in {
                index.name for index in AuditLog.__table__.indexes
            }
        finally:
            await engine.dispose()

    asyncio.run(go())


@pytest.mark.postgres
@pytest.mark.skipif(not os.getenv("ATLAS_POSTGRES_URL"), reason="needs migrated PostgreSQL (ATLAS_POSTGRES_URL)")
def test_concurrent_audit_writers_never_fork_the_chain_on_postgres():
    from app.audit_chain import record_audit
    async def _all():
        from app.db import SessionLocal
        async with SessionLocal() as db:
            return (await db.execute(select(AuditLog.id, AuditLog.previous_hash, AuditLog.event_hash).order_by(AuditLog.id))).all()
    async def go():
        rows_before = await _all()
        res = await asyncio.gather(*[record_audit("PG_PROBE", {"i": i}, "t") for i in range(25)], return_exceptions=True)
        assert not [r for r in res if isinstance(r, Exception)]
        added = (await _all())[len(rows_before):]
        assert len(added) == 25
        assert all(y[1] == x[2] for x, y in zip(added, added[1:])), "audit chain forked"
    asyncio.run(go())
