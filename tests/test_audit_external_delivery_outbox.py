from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

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

        first_claim = await audit_chain.claim_external_audit_delivery(event_hash)
        assert first_claim
        # An unexpired concurrent lease is distinct from an already delivered event.
        assert await audit_chain.claim_external_audit_delivery(event_hash) == (
            audit_chain.EXTERNAL_AUDIT_DELIVERY_IN_FLIGHT
        )

        assert await audit_chain.finish_external_audit_delivery(
            event_hash, claim_token=first_claim,
            delivered=False, error_type="SinkUnavailable"
        )
        second_claim = await audit_chain.claim_external_audit_delivery(event_hash)
        assert second_claim and second_claim != first_claim
        assert await audit_chain.finish_external_audit_delivery(
            event_hash, claim_token=second_claim, delivered=True
        )

        # Once acknowledged, later idempotent order recovery cycles do not resend.
        assert await audit_chain.claim_external_audit_delivery(event_hash) is None

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



def test_expired_audit_claim_cannot_overwrite_a_newer_delivery(monkeypatch):
    async def exercise():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(AuditLogExternalDelivery.__table__.create)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(audit_chain, "SessionLocal", sessions)
        event_hash = "b" * 64

        old_claim = await audit_chain.claim_external_audit_delivery(event_hash)
        assert old_claim
        async with sessions() as db:
            row = await db.get(AuditLogExternalDelivery, event_hash)
            row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()

        new_claim = await audit_chain.claim_external_audit_delivery(event_hash)
        assert new_claim and new_claim != old_claim

        # A late failure from the expired worker has no authority over the
        # replacement lease and cannot put a delivered event back in PENDING.
        assert not await audit_chain.finish_external_audit_delivery(
            event_hash, claim_token=old_claim,
            delivered=False, error_type="LateOldWorkerFailure"
        )
        async with sessions() as db:
            row = await db.get(AuditLogExternalDelivery, event_hash)
            assert row.status == "IN_FLIGHT"
            assert row.claim_token == new_claim

        assert await audit_chain.finish_external_audit_delivery(
            event_hash, claim_token=new_claim, delivered=True
        )
        assert await audit_chain.claim_external_audit_delivery(event_hash) is None

        async with sessions() as db:
            row = await db.get(AuditLogExternalDelivery, event_hash)
            assert row.status == "DELIVERED"
            assert row.attempt_count == 2
            assert row.claim_token is None
        await engine.dispose()

    asyncio.run(exercise())


def test_active_external_audit_lease_can_be_retried_after_competing_worker_failure(monkeypatch):
    async def exercise():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(AuditLogExternalDelivery.__table__.create)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(audit_chain, "SessionLocal", sessions)
        event_hash = "c" * 64

        competing_claim = await audit_chain.claim_external_audit_delivery(event_hash)
        assert competing_claim
        assert await audit_chain.claim_external_audit_delivery(event_hash) == (
            audit_chain.EXTERNAL_AUDIT_DELIVERY_IN_FLIGHT
        )

        # Simulate the competing worker dying before delivery confirmation.
        async with sessions() as db:
            row = await db.get(AuditLogExternalDelivery, event_hash)
            row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()

        recovery_claim = await audit_chain.claim_external_audit_delivery(event_hash)
        assert recovery_claim and recovery_claim != competing_claim
        assert await audit_chain.finish_external_audit_delivery(
            event_hash,
            claim_token=recovery_claim,
            delivered=False,
            error_type="CompetingWorkerFailed",
        )

        # The retry row can now claim the released event and confirm delivery.
        retry_claim = await audit_chain.claim_external_audit_delivery(event_hash)
        assert retry_claim and retry_claim not in {competing_claim, recovery_claim}
        assert await audit_chain.finish_external_audit_delivery(
            event_hash, claim_token=retry_claim, delivered=True
        )

        async with sessions() as db:
            row = await db.get(AuditLogExternalDelivery, event_hash)
            assert row.status == "DELIVERED"
            assert row.attempt_count == 3
            assert row.claim_token is None
        await engine.dispose()

    asyncio.run(exercise())
