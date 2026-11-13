"""Database-backed regression tests for authorized terminal withdrawal ledger repair."""
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.main as main
from app.config import settings
from app.db import Base, CustomerLedgerAccount, CustomerProfile, Incident, Withdrawal
from app.payout import PayoutNotFound


@pytest.fixture(autouse=True)
def _test_encryption_key(monkeypatch):
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "{}")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")


def _db():
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _incident(*, customer_id: int, request_id: str, amount: str = "25.000000") -> Incident:
    reference_id = f"{request_id}:release"
    return Incident(
        incident_key=f"WITHDRAWAL_REPLAY_MISMATCH:withdrawal-release:{reference_id}",
        severity="HIGH",
        status="OPEN",
        category="LEDGER_RECONCILIATION",
        customer_id=customer_id,
        summary="Withdrawal replay mismatch",
        detail_json=json.dumps({
            "action": "release",
            "customer_id": customer_id,
            "reference_id": reference_id,
            "requested_amount": amount,
            "idempotency_key": f"withdrawal-release:{reference_id}",
            "problems": ["release journal lines"],
        }),
    )


async def _call_repair(sessions, withdrawal, monkeypatch, *, provider=None):
    async def fake_auth(*_args, **_kwargs):
        return {"sub": "operator-1", "role": "TREASURY"}

    async def fake_role(*_args, **_kwargs):
        return True

    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(main, "auth", fake_auth)
    monkeypatch.setattr(main, "require_role", fake_role)
    monkeypatch.setattr(main, "approver_auth", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "verify_release_operator", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(main, "release_separation_violation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_audit", AsyncMock())
    monkeypatch.setattr(main, "sync_wallet_from_ledger", AsyncMock())
    if provider is not None:
        monkeypatch.setattr(main, "get_payout_provider", lambda *_args, **_kwargs: provider)
    return await main.repair_withdrawal_ledger(
        withdrawal.id,
        main.WithdrawalNotSentRequest(
            operator_id="operator-1",
            admin_id="approver-1",
            evidence="Two-person review verified the recorded terminal outcome and ledger repair.",
        ),
        x_admin_token="admin",
        x_approver_token="approver-secret",
        x_release_token="operator-secret",
        authorization=None,
    )


def test_rejected_terminal_withdrawal_has_authorized_ledger_repair(monkeypatch):
    async def run():
        engine = _db()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                profile = CustomerProfile(
                    auth_user_id="terminal-repair-rejected-customer",
                    email="",
                    display_name="Terminal repair test",
                )
                db.add(profile)
                await db.flush()
                customer_id = int(profile.id)
                request_id = "terminal-repair-rejected"
                db.add(CustomerLedgerAccount(
                    customer_id=customer_id, currency="USDT",
                    available=Decimal("0"), trading_reserved=Decimal("0"),
                    withdrawal_reserved=Decimal("25"),
                ))
                withdrawal = Withdrawal(
                    customer_id=customer_id, wallet_id=None, request_id=request_id,
                    amount=Decimal("25"), currency="USDT", provider="",
                    provider_id="", status="REJECTED", rejected_by="approver-1",
                    rejected_at=datetime.now(timezone.utc),
                    rejection_reason="Treasury rejected before provider execution.",
                )
                db.add(withdrawal)
                db.add(_incident(customer_id=customer_id, request_id=request_id))
                await db.commit()
                await db.refresh(withdrawal)

            result = await _call_repair(sessions, withdrawal, monkeypatch)

            assert result["ok"] is True
            assert result["status"] == "REJECTED"
            assert result["ledger_action"] == "release"
            assert result["incident_status"] == "RESOLVED"
            assert result["provider_send_attempted"] is False

            async with sessions() as db:
                ledger = (
                    await db.execute(
                        select(CustomerLedgerAccount).where(
                            CustomerLedgerAccount.customer_id == customer_id,
                            CustomerLedgerAccount.currency == "USDT",
                        )
                    )
                ).scalar_one()
                incident = (
                    await db.execute(
                        select(Incident).where(
                            Incident.incident_key.like(
                                "WITHDRAWAL_REPLAY_MISMATCH:withdrawal-release:%"
                            )
                        )
                    )
                ).scalar_one()
                saved = await db.get(Withdrawal, withdrawal.id)
                assert saved.status == "REJECTED"
                assert ledger.available == Decimal("25.000000")
                assert ledger.withdrawal_reserved == Decimal("0.000000")
                assert incident.status == "RESOLVED"
                assert incident.resolved_by == "operator-1"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_failed_terminal_repair_requires_provider_recovery_and_never_sends(monkeypatch):
    async def run():
        engine = _db()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                profile = CustomerProfile(
                    auth_user_id="terminal-repair-failed-customer",
                    email="",
                    display_name="Terminal repair failed test",
                )
                db.add(profile)
                await db.flush()
                customer_id = int(profile.id)
                request_id = "terminal-repair-failed"
                db.add(CustomerLedgerAccount(
                    customer_id=customer_id, currency="USDT",
                    available=Decimal("0"), trading_reserved=Decimal("0"),
                    withdrawal_reserved=Decimal("25"),
                ))
                withdrawal = Withdrawal(
                    customer_id=customer_id, wallet_id=None, request_id=request_id,
                    amount=Decimal("25"), currency="USDT", provider="external_signer",
                    provider_id="", status="FAILED",
                    execution_started_at=datetime.now(timezone.utc),
                    provider_error="Provider definitively failed before assigning an ID.",
                )
                db.add(withdrawal)
                db.add(_incident(customer_id=customer_id, request_id=request_id))
                await db.commit()
                await db.refresh(withdrawal)

            class Provider:
                send = AsyncMock(side_effect=AssertionError("repair must never send a payout"))

                async def recover(self, *_args, **_kwargs):
                    raise PayoutNotFound("Provider confirms no payout exists.")

            provider = Provider()
            result = await _call_repair(
                sessions, withdrawal, monkeypatch, provider=provider
            )
            assert result["ok"] is True
            assert result["provider_status"] == "NOT_SENT"
            assert result["provider_send_attempted"] is False
            provider.send.assert_not_awaited()

            async with sessions() as db:
                ledger = (
                    await db.execute(
                        select(CustomerLedgerAccount).where(
                            CustomerLedgerAccount.customer_id == customer_id,
                            CustomerLedgerAccount.currency == "USDT",
                        )
                    )
                ).scalar_one()
                saved = await db.get(Withdrawal, withdrawal.id)
                assert saved.status == "FAILED"
                assert saved.provider_status == "NOT_SENT"
                assert ledger.withdrawal_reserved == Decimal("0.000000")
                assert ledger.available == Decimal("25.000000")
        finally:
            await engine.dispose()

    asyncio.run(run())
