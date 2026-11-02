from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import audit_chain
from app.db import AuditLogExternalDelivery


def test_external_audit_delivery_outbox_retries_failure_and_never_resends_success(monkeypatch):
    async def exercise():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(AuditLogExternalDelivery.__table__.create)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(audit_chain, "SessionLocal", sessions)
        event_hash = "a" * 64

        assert await audit_chain.claim_external_audit_delivery(event_hash)
        # An unexpired concurrent lease prevents a duplicate send.
        assert not await audit_chain.claim_external_audit_delivery(event_hash)

        await audit_chain.finish_external_audit_delivery(
            event_hash, delivered=False, error_type="SinkUnavailable"
        )
        assert await audit_chain.claim_external_audit_delivery(event_hash)
        await audit_chain.finish_external_audit_delivery(event_hash, delivered=True)

        # Once acknowledged, later idempotent order recovery cycles do not resend.
        assert not await audit_chain.claim_external_audit_delivery(event_hash)

        async with sessions() as db:
            row = await db.get(AuditLogExternalDelivery, event_hash)
            assert row is not None
            assert row.status == "DELIVERED"
            assert row.attempt_count == 2
            assert row.delivered_at is not None
            assert row.lease_until is None
            assert row.last_error_type is None
        await engine.dispose()

    asyncio.run(exercise())
