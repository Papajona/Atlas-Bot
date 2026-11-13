"""Database-backed regression tests for authorized terminal withdrawal ledger repair."""
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.customer_funds as customer_funds
import app.main as main
from app.config import settings
from app.customer_funds import WithdrawalReplayMismatch, persist_withdrawal_replay_mismatch
from app.db import (
    Base, CustomerLedgerAccount, CustomerProfile, Incident, LedgerEntry,
    LedgerJournal, LedgerJournalLine, Withdrawal,
)
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


async def _seed_release_journal(
    db, *, customer_id: int, request_id: str, amount: str = "25.000000",
    corrupt: bool = False,
) -> None:
    """Seed a funded, reserved withdrawal and its release/correction journal."""
    reference_id = f"{request_id}:release"
    idempotency_key = f"withdrawal-release:{reference_id}"
    requested = Decimal(amount)
    posted = Decimal("24.000000") if corrupt else requested

    # Initial funding -> available; withdrawal reserve -> reserved; release -> available.
    # This lets the account-balance verifier reconstruct the exact post-release state.
    opening = LedgerJournal(
        currency="USDT", entry_type="OPENING_BALANCE",
        reference_type="LEGACY_WALLET", reference_id=f"wallet:{customer_id}",
        idempotency_key=f"opening:{customer_id}:USDT",
        description="Opening funds for withdrawal repair fixture",
    )
    reserve = LedgerJournal(
        currency="USDT", entry_type="WITHDRAWAL_RESERVE",
        reference_type="WITHDRAWAL", reference_id=request_id,
        idempotency_key=f"withdrawal-reserve:{request_id}",
        description="Original withdrawal reserve fixture",
    )
    db.add_all([opening, reserve])
    await db.flush()
    release = LedgerJournal(
        currency="USDT",
        entry_type="WITHDRAWAL_RELEASE",
        reference_type="WITHDRAWAL",
        reference_id=reference_id,
        idempotency_key=idempotency_key,
        description="Operator-reviewed withdrawal ledger repair fixture",
    )
    db.add(release)
    await db.flush()
    db.add_all([
        LedgerJournalLine(
            journal_id=opening.id, line_no=1,
            account_code="ASSET:LEGACY:WALLET_MIGRATION",
            customer_id=None, currency="USDT", debit=requested, credit=0,
        ),
        LedgerJournalLine(
            journal_id=opening.id, line_no=2,
            account_code=f"LIABILITY:CUSTOMER:{customer_id}:USDT:AVAILABLE",
            customer_id=customer_id, currency="USDT", debit=0, credit=requested,
        ),
        LedgerJournalLine(
            journal_id=reserve.id, line_no=1,
            account_code=f"LIABILITY:CUSTOMER:{customer_id}:USDT:AVAILABLE",
            customer_id=customer_id, currency="USDT", debit=requested, credit=0,
        ),
        LedgerJournalLine(
            journal_id=reserve.id, line_no=2,
            account_code=f"LIABILITY:CUSTOMER:{customer_id}:USDT:WITHDRAWAL_RESERVED",
            customer_id=customer_id, currency="USDT", debit=0, credit=requested,
        ),
        LedgerJournalLine(
            journal_id=release.id, line_no=1,
            account_code=f"LIABILITY:CUSTOMER:{customer_id}:USDT:WITHDRAWAL_RESERVED",
            customer_id=customer_id, currency="USDT", debit=posted, credit=0,
        ),
        LedgerJournalLine(
            journal_id=release.id, line_no=2,
            account_code=f"LIABILITY:CUSTOMER:{customer_id}:USDT:AVAILABLE",
            customer_id=customer_id, currency="USDT", debit=0, credit=posted,
        ),
        LedgerEntry(
            customer_id=customer_id, currency="USDT", entry_type="OPENING_BALANCE",
            debit=0, credit=requested, amount=requested,
            reference_type="LEGACY_WALLET", reference_id=f"wallet:{customer_id}",
            idempotency_key=f"opening:{customer_id}:USDT",
        ),
        LedgerEntry(
            customer_id=customer_id, currency="USDT", entry_type="WITHDRAWAL_RESERVE",
            debit=requested, credit=0, amount=requested,
            reference_type="WITHDRAWAL", reference_id=request_id,
            idempotency_key=f"withdrawal-reserve:{request_id}",
        ),
        LedgerEntry(
            customer_id=customer_id, currency="USDT",
            entry_type="WITHDRAWAL_RELEASE", debit=0, credit=requested,
            amount=requested, reference_type="WITHDRAWAL",
            reference_id=reference_id, idempotency_key=idempotency_key,
            metadata_json=json.dumps({
                "requested_amount": format(requested, "f"),
                "effective_amount": format(requested, "f"),
            }),
        ),
    ])

def _replay_mismatch(customer_id: int, request_id: str) -> WithdrawalReplayMismatch:
    reference_id = f"{request_id}:release"
    return WithdrawalReplayMismatch({
        "key": f"WITHDRAWAL_REPLAY_MISMATCH:withdrawal-release:{reference_id}",
        "summary": "Withdrawal idempotency key replayed with a different or inconsistent operation",
        "detail": {
            "action": "release",
            "customer_id": customer_id,
            "reference_id": reference_id,
            "requested_amount": "25.000000",
            "idempotency_key": f"withdrawal-release:{reference_id}",
            "problems": ["release journal lines"],
        },
        "customer_id": customer_id,
        "severity": "HIGH",
    })


async def _call_repair(sessions, withdrawal, monkeypatch, *, provider=None, sync_wallet=None):
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
    monkeypatch.setattr(main, "sync_wallet_from_ledger", sync_wallet or AsyncMock())
    if provider is not None:
        monkeypatch.setattr(main, "get_payout_provider", lambda *_args, **_kwargs: provider)
    return await main.repair_withdrawal_ledger(
        withdrawal.id,
        main.WithdrawalLedgerRepairRequest(
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
                    available=Decimal("25"), trading_reserved=Decimal("0"),
                    withdrawal_reserved=Decimal("0"),
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
                await _seed_release_journal(
                    db, customer_id=customer_id, request_id=request_id
                )
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
                    available=Decimal("25"), trading_reserved=Decimal("0"),
                    withdrawal_reserved=Decimal("0"),
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
                await _seed_release_journal(
                    db, customer_id=customer_id, request_id=request_id
                )
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


def test_repair_refuses_a_real_unfixed_replay_journal(monkeypatch):
    async def run():
        engine = _db()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                profile = CustomerProfile(
                    auth_user_id="terminal-repair-real-conflict-customer",
                    email="",
                    display_name="Terminal repair conflict test",
                )
                db.add(profile)
                await db.flush()
                customer_id = int(profile.id)
                request_id = "terminal-repair-real-conflict"
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
                await _seed_release_journal(
                    db, customer_id=customer_id, request_id=request_id, corrupt=True
                )
                await db.commit()
                await db.refresh(withdrawal)

            class Provider:
                send = AsyncMock(side_effect=AssertionError("repair must never send a payout"))

                async def recover(self, *_args, **_kwargs):
                    raise PayoutNotFound("Provider confirms no payout exists.")

            with pytest.raises(main.HTTPException) as blocked:
                await _call_repair(
                    sessions, withdrawal, monkeypatch, provider=Provider()
                )
            assert blocked.value.status_code == 409

            async with sessions() as db:
                saved = await db.get(Withdrawal, withdrawal.id)
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
                            Incident.incident_key == (
                                f"WITHDRAWAL_REPLAY_MISMATCH:withdrawal-release:{request_id}:release"
                            )
                        )
                    )
                ).scalar_one()
                assert saved.status == "FAILED"
                assert ledger.available == Decimal("0.000000")
                assert ledger.withdrawal_reserved == Decimal("25.000000")
                assert incident.status == "OPEN"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_replay_incident_write_failure_rolls_back_terminal_withdrawal_too(monkeypatch):
    async def run():
        engine = _db()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                profile = CustomerProfile(
                    auth_user_id="atomic-replay-incident-customer",
                    email="",
                    display_name="Atomic replay incident test",
                )
                db.add(profile)
                await db.flush()
                customer_id = int(profile.id)
                withdrawal = Withdrawal(
                    customer_id=customer_id, wallet_id=None,
                    request_id="atomic-replay-incident",
                    amount=Decimal("25"), currency="USDT", status="SUBMITTING",
                )
                db.add(withdrawal)
                await db.commit()
                withdrawal_id = withdrawal.id

            async def fail_incident_write(*_args, **_kwargs):
                raise RuntimeError("simulated incident-store outage")

            monkeypatch.setattr(
                customer_funds, "record_ledger_incident", fail_incident_write
            )
            async with sessions() as db:
                withdrawal = await db.get(Withdrawal, withdrawal_id)
                withdrawal.status = "FAILED"
                with pytest.raises(RuntimeError, match="incident-store outage"):
                    await persist_withdrawal_replay_mismatch(
                        db, _replay_mismatch(customer_id, "atomic-replay-incident")
                    )
                await db.rollback()

            async with sessions() as db:
                saved = await db.get(Withdrawal, withdrawal_id)
                incident = (
                    await db.execute(
                        select(Incident).where(
                            Incident.incident_key == (
                                "WITHDRAWAL_REPLAY_MISMATCH:"
                                "withdrawal-release:atomic-replay-incident:release"
                            )
                        )
                    )
                ).scalar_one_or_none()
                assert saved.status == "SUBMITTING"
                assert incident is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_duplicate_replay_incident_race_preserves_async_session_objects(monkeypatch):
    async def run():
        engine = _db()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            request_id = "duplicate-replay-incident"
            async with sessions() as db:
                profile = CustomerProfile(
                    auth_user_id="duplicate-replay-incident-customer",
                    email="",
                    display_name="Duplicate replay incident test",
                )
                db.add(profile)
                await db.flush()
                customer_id = int(profile.id)
                withdrawal = Withdrawal(
                    customer_id=customer_id, wallet_id=None, request_id=request_id,
                    amount=Decimal("25"), currency="USDT", status="SUBMITTING",
                )
                db.add(withdrawal)
                # A competing request has already won the unique incident-key race.
                db.add(_incident(customer_id=customer_id, request_id=request_id))
                await db.commit()
                withdrawal_id = withdrawal.id

            original_record = customer_funds.record_ledger_incident
            calls = 0

            async def simulate_unique_key_race(db, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise IntegrityError(
                        "INSERT INTO incident", {}, RuntimeError("duplicate incident key")
                    )
                return await original_record(db, **kwargs)

            monkeypatch.setattr(
                customer_funds, "record_ledger_incident", simulate_unique_key_race
            )
            async with sessions() as db:
                withdrawal = await db.get(Withdrawal, withdrawal_id)
                withdrawal.status = "FAILED"
                await persist_withdrawal_replay_mismatch(
                    db, _replay_mismatch(customer_id, request_id)
                )
                # This access must not trigger an implicit async lazy load.
                assert withdrawal.status == "FAILED"
                assert withdrawal.id == withdrawal_id
                await db.commit()
            assert calls == 2

            async with sessions() as db:
                saved = await db.get(Withdrawal, withdrawal_id)
                incident = (
                    await db.execute(
                        select(Incident).where(
                            Incident.incident_key == (
                                f"WITHDRAWAL_REPLAY_MISMATCH:"
                                f"withdrawal-release:{request_id}:release"
                            )
                        )
                    )
                ).scalar_one()
                assert saved.status == "FAILED"
                assert incident.status == "OPEN"
                assert incident.severity == "HIGH"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repair_rejects_valid_journal_when_customer_reserve_is_still_locked(monkeypatch):
    async def run():
        engine = _db()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                profile = CustomerProfile(
                    auth_user_id="terminal-repair-stale-reserve-customer",
                    email="",
                    display_name="Terminal repair stale reserve test",
                )
                db.add(profile)
                await db.flush()
                customer_id = int(profile.id)
                request_id = "terminal-repair-stale-reserve"
                # Journal reflects the manual release, but the authoritative account
                # still holds this withdrawal's reserve.
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
                await _seed_release_journal(
                    db, customer_id=customer_id, request_id=request_id
                )
                await db.commit()
                await db.refresh(withdrawal)

            class Provider:
                send = AsyncMock(side_effect=AssertionError("repair must never send a payout"))

                async def recover(self, *_args, **_kwargs):
                    raise PayoutNotFound("Provider confirms no payout exists.")

            sync_wallet = AsyncMock()
            with pytest.raises(main.HTTPException) as blocked:
                await _call_repair(
                    sessions, withdrawal, monkeypatch,
                    provider=Provider(), sync_wallet=sync_wallet,
                )
            assert blocked.value.status_code == 409
            sync_wallet.assert_not_awaited()

            async with sessions() as db:
                saved = await db.get(Withdrawal, withdrawal.id)
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
                assert saved.status == "FAILED"
                assert saved.provider_status in (None, "")
                assert ledger.available == Decimal("0.000000")
                assert ledger.withdrawal_reserved == Decimal("25.000000")
                assert incident.status == "OPEN"
        finally:
            await engine.dispose()

    asyncio.run(run())
