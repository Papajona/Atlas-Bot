"""Custody/withdrawal acceptance against the selected isolated database.

No blockchain, exchange withdrawal, or signing key is contacted. The mandatory
PostgreSQL workflow supplies ATLAS_POSTGRES_URL so this exact test exercises
the migrated PostgreSQL ledger; ordinary local/SQLite suites retain a fast
in-memory fallback.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from unittest import mock
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.customer_funds import (
    DEFICIT_ACCOUNT,
    customer_balance,
    ledger_invariant_report,
    post_deposit,
    reserve_withdrawal,
    release_withdrawal,
    settle_withdrawal,
)
from app.db import (
    Base,
    CustomerLedgerAccount,
    CustomerProfile,
    Incident,
    LedgerEntry,
    LedgerJournal,
    LedgerJournalLine,
    Withdrawal,
)


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")


async def _assert_journal(
    db,
    *,
    idempotency_key: str,
    entry_type: str,
    reference_id: str,
    expected_lines: list[tuple[str, int | None, Decimal, Decimal]],
) -> None:
    journal = (
        await db.execute(
            select(LedgerJournal).where(LedgerJournal.idempotency_key == idempotency_key)
        )
    ).scalar_one()
    assert journal.entry_type == entry_type
    assert journal.reference_type in {"TRON_TX", "WITHDRAWAL"}
    assert journal.reference_id == reference_id

    rows = (
        await db.execute(
            select(LedgerJournalLine)
            .where(LedgerJournalLine.journal_id == journal.id)
            .order_by(LedgerJournalLine.line_no)
        )
    ).scalars().all()
    actual = [
        (
            row.account_code,
            int(row.customer_id) if row.customer_id is not None else None,
            Decimal(str(row.debit)),
            Decimal(str(row.credit)),
        )
        for row in rows
    ]
    assert actual == expected_lines


def test_custody_withdrawal_acceptance_round_trip_is_idempotent():
    async def run():
        # The mandatory ledger workflow sets both URLs to the isolated PostgreSQL
        # service after applying the repository's migrations. SQLite is only the
        # fallback for the ordinary developer test suite.
        run_token = uuid.uuid4().hex
        sqlite_path = os.path.join(tempfile.gettempdir(), f"atlas-custody-acceptance-{run_token}.db")
        database_url = os.environ.get("ATLAS_POSTGRES_URL") or f"sqlite+aiosqlite:///{sqlite_path}"
        is_sqlite = database_url.startswith("sqlite")
        engine = create_async_engine(database_url, pool_pre_ping=True)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        customer_id: int | None = None
        wallet_id = 7001
        deposit_ref = f"acceptance-deposit-{run_token}"
        reserve_one_ref = f"acceptance-reserve-one-{run_token}"
        release_ref = f"acceptance-release-{run_token}"
        reserve_two_ref = f"acceptance-reserve-two-{run_token}"
        cleanup_keys = [
            f"deposit:test-custody:{deposit_ref}",
            f"withdrawal-reserve:{reserve_one_ref}",
            f"withdrawal-release:{release_ref}",
            f"withdrawal-reserve:{reserve_two_ref}",
            f"withdrawal-settlement:{reserve_two_ref}",
        ]
        try:
            if is_sqlite:
                async with engine.connect() as conn:
                    await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
                    await conn.commit()
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)

            async with sessions() as db:
                try:
                    profile = CustomerProfile(
                        auth_user_id=f"custody-acceptance-{run_token}",
                        email="",
                        display_name="Custody acceptance",
                    )
                    db.add(profile)
                    await db.flush()
                    customer_id = int(profile.id)
                    db.add(
                        CustomerLedgerAccount(
                            customer_id=customer_id,
                            currency="USDT",
                            available=Decimal("0"),
                            trading_reserved=Decimal("0"),
                            withdrawal_reserved=Decimal("0"),
                        )
                    )
                    await db.commit()

                    # 1. Confirmed custody deposit -> authoritative customer ledger.
                    await post_deposit(
                        db,
                        customer_id=customer_id,
                        wallet_id=wallet_id,
                        amount=100,
                        provider_reference=deposit_ref,
                        metadata={"harness": "custody-withdrawal"},
                        provider="test-custody",
                    )
                    await db.commit()

                    # Duplicate provider delivery must not mint another balance.
                    await post_deposit(
                        db,
                        customer_id=customer_id,
                        wallet_id=wallet_id,
                        amount=100,
                        provider_reference=deposit_ref,
                        metadata={"harness": "custody-withdrawal", "replay": True},
                        provider="test-custody",
                    )
                    await db.commit()

                    balance = await customer_balance(db, customer_id)
                    assert balance["available_exact"] == "100.000000"
                    assert balance["trading_reserved_exact"] == "0.000000"
                    assert balance["withdrawal_reserved_exact"] == "0.000000"
                    assert balance["total_exact"] == "100.000000"

                    # 2. Withdrawal authorization boundary: reserve before release.
                    await reserve_withdrawal(
                        db, customer_id, 25, reference_id=reserve_one_ref
                    )
                    await db.commit()
                    balance = await customer_balance(db, customer_id)
                    assert balance["available_exact"] == "75.000000"
                    assert balance["withdrawal_reserved_exact"] == "25.000000"

                    # 3. Simulated failed payout releases the exact reserve.
                    await release_withdrawal(
                        db, customer_id, 25, reference_id=release_ref
                    )
                    await db.commit()
                    # Exact retries remain idempotent; a changed amount must not
                    # silently reuse the original release journal.
                    await release_withdrawal(
                        db, customer_id, 25, reference_id=release_ref
                    )
                    await db.commit()
                    with mock.patch("app.customer_funds.SessionLocal", sessions):
                        with pytest.raises(ValueError, match="different or inconsistent operation"):
                            await release_withdrawal(
                                db, customer_id, 26, reference_id=release_ref
                            )
                    # The validator rolls back the rejected action, commits its
                    # incident in a fresh transaction, then raises. An additional
                    # caller rollback must not erase that incident.
                    await db.rollback()
                    release_incident = (
                        await db.execute(
                            select(Incident).where(
                                Incident.incident_key
                                == f"WITHDRAWAL_REPLAY_MISMATCH:withdrawal-release:{release_ref}"
                            )
                        )
                    ).scalar_one()
                    assert release_incident.severity == "HIGH"
                    assert release_incident.status == "OPEN"
                    balance = await customer_balance(db, customer_id)
                    assert balance["available_exact"] == "100.000000"
                    assert balance["withdrawal_reserved_exact"] == "0.000000"

                    # 4. Re-reserve and simulate a completed payout.
                    await reserve_withdrawal(
                        db, customer_id, 30, reference_id=reserve_two_ref
                    )
                    await db.commit()
                    await settle_withdrawal(
                        db, customer_id, 30, reference_id=reserve_two_ref
                    )
                    await db.commit()
                    # The matching replay is a no-op; changing the payout amount
                    # under the same idempotency key is rejected.
                    await settle_withdrawal(
                        db, customer_id, 30, reference_id=reserve_two_ref
                    )
                    await db.commit()
                    with mock.patch("app.customer_funds.SessionLocal", sessions):
                        with pytest.raises(ValueError, match="different or inconsistent operation"):
                            await settle_withdrawal(
                                db, customer_id, 31, reference_id=reserve_two_ref
                            )
                    await db.rollback()
                    settlement_incident = (
                        await db.execute(
                            select(Incident).where(
                                Incident.incident_key
                                == f"WITHDRAWAL_REPLAY_MISMATCH:withdrawal-settlement:{reserve_two_ref}"
                            )
                        )
                    ).scalar_one()
                    assert settlement_incident.severity == "HIGH"
                    assert settlement_incident.status == "OPEN"

                    balance = await customer_balance(db, customer_id)
                    assert balance["available_exact"] == "70.000000"
                    assert balance["withdrawal_reserved_exact"] == "0.000000"
                    assert balance["total_exact"] == "70.000000"

                    # Idempotent deposit replay creates one ledger entry and one journal.
                    entries = (
                        await db.execute(
                            select(LedgerEntry)
                            .where(LedgerEntry.customer_id == customer_id)
                            .order_by(LedgerEntry.id)
                        )
                    ).scalars().all()
                    types = [row.entry_type for row in entries]
                    assert types.count("DEPOSIT_CREDIT") == 1
                    assert types.count("WITHDRAWAL_RESERVE") == 2
                    assert types.count("WITHDRAWAL_RELEASE") == 1
                    assert types.count("WITHDRAWAL_SETTLEMENT") == 1

                    journals = (
                        await db.execute(
                            select(LedgerJournal)
                            .where(LedgerJournal.idempotency_key.in_(cleanup_keys))
                            .order_by(LedgerJournal.id)
                        )
                    ).scalars().all()
                    assert [row.idempotency_key for row in journals] == cleanup_keys
                    assert len({row.idempotency_key for row in journals}) == len(cleanup_keys)

                    # Verify every operation independently. Aggregate end-state
                    # reconciliation alone can hide equal-and-opposite posting errors.
                    await _assert_journal(
                        db,
                        idempotency_key=cleanup_keys[0],
                        entry_type="DEPOSIT_CREDIT",
                        reference_id=deposit_ref,
                        expected_lines=[
                            (f"ASSET:TRON:DEPOSIT:{wallet_id}", None, Decimal("100"), Decimal("0")),
                            (f"LIABILITY:CUSTOMER:{customer_id}:USDT:AVAILABLE", customer_id, Decimal("0"), Decimal("100")),
                        ],
                    )
                    await _assert_journal(
                        db,
                        idempotency_key=cleanup_keys[1],
                        entry_type="WITHDRAWAL_RESERVE",
                        reference_id=reserve_one_ref,
                        expected_lines=[
                            (f"LIABILITY:CUSTOMER:{customer_id}:USDT:AVAILABLE", customer_id, Decimal("25"), Decimal("0")),
                            (f"LIABILITY:CUSTOMER:{customer_id}:USDT:WITHDRAWAL_RESERVED", customer_id, Decimal("0"), Decimal("25")),
                        ],
                    )
                    await _assert_journal(
                        db,
                        idempotency_key=cleanup_keys[2],
                        entry_type="WITHDRAWAL_RELEASE",
                        reference_id=release_ref,
                        expected_lines=[
                            (f"LIABILITY:CUSTOMER:{customer_id}:USDT:WITHDRAWAL_RESERVED", customer_id, Decimal("25"), Decimal("0")),
                            (f"LIABILITY:CUSTOMER:{customer_id}:USDT:AVAILABLE", customer_id, Decimal("0"), Decimal("25")),
                        ],
                    )
                    await _assert_journal(
                        db,
                        idempotency_key=cleanup_keys[3],
                        entry_type="WITHDRAWAL_RESERVE",
                        reference_id=reserve_two_ref,
                        expected_lines=[
                            (f"LIABILITY:CUSTOMER:{customer_id}:USDT:AVAILABLE", customer_id, Decimal("30"), Decimal("0")),
                            (f"LIABILITY:CUSTOMER:{customer_id}:USDT:WITHDRAWAL_RESERVED", customer_id, Decimal("0"), Decimal("30")),
                        ],
                    )
                    await _assert_journal(
                        db,
                        idempotency_key=cleanup_keys[4],
                        entry_type="WITHDRAWAL_SETTLEMENT",
                        reference_id=reserve_two_ref,
                        expected_lines=[
                            (f"LIABILITY:CUSTOMER:{customer_id}:USDT:WITHDRAWAL_RESERVED", customer_id, Decimal("30"), Decimal("0")),
                            ("ASSET:TRON:WITHDRAWAL", None, Decimal("0"), Decimal("30")),
                        ],
                    )

                    invariant = await ledger_invariant_report(db, customer_id=customer_id)
                    assert invariant["ok"], invariant
                    assert invariant["unbalanced_journals"] == []
                    assert invariant["bucket_drifts"] == []
                    assert invariant["customer_account_mismatches"] == []
                finally:
                    # Ledger journals are append-only by database trigger. This is an
                    # isolated disposable CI database, so keep immutable evidence;
                    # per-run UUID keys prevent collisions with other test executions.
                    await db.rollback()
        finally:
            await engine.dispose()
            if is_sqlite:
                for suffix in ("", "-wal", "-shm"):
                    try:
                        os.remove(sqlite_path + suffix)
                    except FileNotFoundError:
                        pass

    asyncio.run(run())


def test_ledger_invariant_report_detects_embedded_customer_account_mismatch():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                profile = CustomerProfile(
                    auth_user_id=f"custody-account-mismatch-{uuid.uuid4().hex}",
                    email="",
                    display_name="Mismatch regression",
                )
                db.add(profile)
                await db.flush()
                customer_id = int(profile.id)
                db.add(
                    CustomerLedgerAccount(
                        customer_id=customer_id,
                        currency="USDT",
                        available=Decimal("10"),
                        trading_reserved=Decimal("0"),
                        withdrawal_reserved=Decimal("0"),
                    )
                )
                await db.flush()
                journal = LedgerJournal(
                    currency="USDT",
                    entry_type="TEST_MISMATCH",
                    reference_type="TEST",
                    reference_id="embedded-customer-mismatch",
                    idempotency_key=f"test-mismatch-{uuid.uuid4().hex}",
                    description="Deliberately mismatched liability customer for invariant regression",
                )
                db.add(journal)
                await db.flush()
                db.add_all([
                    LedgerJournalLine(
                        journal_id=journal.id,
                        line_no=1,
                        account_code="ASSET:TEST:DEPOSIT",
                        customer_id=None,
                        currency="USDT",
                        debit=Decimal("20"),
                        credit=Decimal("0"),
                    ),
                    LedgerJournalLine(
                        journal_id=journal.id,
                        line_no=2,
                        account_code=f"LIABILITY:CUSTOMER:{customer_id + 1}:USDT:AVAILABLE",
                        customer_id=customer_id,
                        currency="USDT",
                        debit=Decimal("0"),
                        credit=Decimal("10"),
                    ),
                    LedgerJournalLine(
                        journal_id=journal.id,
                        line_no=3,
                        account_code=f"LIABILITY:CUSTOMER:{customer_id}:USDT:AVAILABLE",
                        customer_id=None,
                        currency="USDT",
                        debit=Decimal("0"),
                        credit=Decimal("10"),
                    ),
                ])
                await db.commit()

                report = await ledger_invariant_report(db, customer_id=customer_id)
                assert report["ok"] is False
                assert report["unbalanced_journals"] == []
                assert report["bucket_drifts"] == []
                assert report["customer_account_mismatch_count"] == 2
                assert report["customer_account_mismatches"] == [
                    {
                        "journal_customer_id": None,
                        "account_code": f"LIABILITY:CUSTOMER:{customer_id}:USDT:AVAILABLE",
                        "account_customer_id": customer_id,
                    },
                    {
                        "journal_customer_id": customer_id,
                        "account_code": f"LIABILITY:CUSTOMER:{customer_id + 1}:USDT:AVAILABLE",
                        "account_customer_id": customer_id + 1,
                    },
                ]
        finally:
            await engine.dispose()

    asyncio.run(run())

@pytest.mark.parametrize(
    "reserved_debit,deficit_debit,metadata_reserved,metadata_shortfall",
    [
        (Decimal("20"), Decimal("10"), Decimal("20"), Decimal("10")),
        (Decimal("-5"), Decimal("35"), Decimal("-5"), Decimal("35")),
    ],
)
def test_settlement_replay_rejects_invalid_debit_components_and_persists_incident(
    reserved_debit, deficit_debit, metadata_reserved, metadata_shortfall
):
    async def run():
        sqlite_path = os.path.join(tempfile.gettempdir(), f"atlas-corrupt-settlement-{uuid.uuid4().hex}.db")
        engine = create_async_engine(f"sqlite+aiosqlite:///{sqlite_path}", pool_pre_ping=True)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        reference_id = f"corrupt-settlement-debit-split-{uuid.uuid4().hex}"
        idem = f"withdrawal-settlement:{reference_id}"
        try:
            async with engine.connect() as conn:
                await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
                await conn.commit()
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with sessions() as db:
                profile = CustomerProfile(
                    auth_user_id=f"corrupt-settlement-debit-split-{uuid.uuid4().hex}",
                    email="",
                    display_name="Replay validation test",
                )
                db.add(profile)
                await db.flush()
                customer_id = int(profile.id)
                db.add(
                    CustomerLedgerAccount(
                        customer_id=customer_id,
                        currency="USDT",
                        available=0,
                        trading_reserved=0,
                        withdrawal_reserved=0,
                    )
                )
                journal = LedgerJournal(
                    currency="USDT",
                    entry_type="WITHDRAWAL_SETTLEMENT",
                    reference_type="WITHDRAWAL",
                    reference_id=reference_id,
                    idempotency_key=idem,
                    description="Deliberately inconsistent historical debit split",
                )
                db.add(journal)
                await db.flush()
                db.add_all(
                    [
                        LedgerJournalLine(
                            journal_id=journal.id,
                            line_no=1,
                            account_code=f"LIABILITY:CUSTOMER:{customer_id}:USDT:WITHDRAWAL_RESERVED",
                            customer_id=customer_id,
                            currency="USDT",
                            debit=reserved_debit,
                            credit=0,
                        ),
                        LedgerJournalLine(
                            journal_id=journal.id,
                            line_no=2,
                            account_code=DEFICIT_ACCOUNT,
                            customer_id=customer_id,
                            currency="USDT",
                            debit=deficit_debit,
                            credit=0,
                        ),
                        LedgerJournalLine(
                            journal_id=journal.id,
                            line_no=3,
                            account_code="ASSET:TRON:WITHDRAWAL",
                            customer_id=None,
                            currency="USDT",
                            debit=0,
                            credit=30,
                        ),
                    ]
                )
                db.add(
                    LedgerEntry(
                        customer_id=customer_id,
                        currency="USDT",
                        entry_type="WITHDRAWAL_SETTLEMENT",
                        debit=30,
                        credit=0,
                        amount=30,
                        reference_type="WITHDRAWAL",
                        reference_id=reference_id,
                        idempotency_key=idem,
                        metadata_json=json.dumps(
                            {
                                "requested_amount": "30.000000",
                                "settled_from_reserve": format(metadata_reserved, "f"),
                                "shortfall": format(metadata_shortfall, "f"),
                            },
                            separators=(",", ":"),
                        ),
                    )
                )
                await db.commit()

                with mock.patch("app.customer_funds.SessionLocal", sessions):
                    with pytest.raises(ValueError, match="different or inconsistent operation"):
                        await settle_withdrawal(
                            db, customer_id, 30, reference_id=reference_id
                        )
                await db.rollback()

                incident = (
                    await db.execute(
                        select(Incident).where(
                            Incident.incident_key
                            == f"WITHDRAWAL_REPLAY_MISMATCH:{idem}"
                        )
                    )
                ).scalar_one()
                assert incident.severity == "HIGH"
                assert incident.status == "OPEN"
                detail = json.loads(incident.detail_json)
                assert "settlement journal lines" in detail["problems"]
        finally:
            await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(sqlite_path + suffix)
                except FileNotFoundError:
                    pass

    asyncio.run(run())


def test_legacy_partial_release_retry_uses_original_withdrawal_amount():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        request_id = "legacy-partial-release-request"
        reference_id = f"{request_id}:release"
        idem = f"withdrawal-release:{reference_id}"
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with sessions() as db:
                profile = CustomerProfile(
                    auth_user_id="legacy-partial-release-test",
                    email="",
                    display_name="Legacy partial release test",
                )
                db.add(profile)
                await db.flush()
                customer_id = int(profile.id)
                db.add(
                    CustomerLedgerAccount(
                        customer_id=customer_id,
                        currency="USDT",
                        available=Decimal("100"),
                        trading_reserved=Decimal("0"),
                        withdrawal_reserved=Decimal("0"),
                    )
                )
                db.add(
                    Withdrawal(
                        customer_id=customer_id,
                        wallet_id=None,
                        request_id=request_id,
                        amount=Decimal("30"),
                        currency="USDT",
                        status="FAILED",
                    )
                )
                journal = LedgerJournal(
                    currency="USDT",
                    entry_type="WITHDRAWAL_RELEASE",
                    reference_type="WITHDRAWAL",
                    reference_id=reference_id,
                    idempotency_key=idem,
                    description="Legacy partial release without requested-amount metadata",
                )
                db.add(journal)
                await db.flush()
                db.add_all(
                    [
                        LedgerJournalLine(
                            journal_id=journal.id,
                            line_no=1,
                            account_code=f"LIABILITY:CUSTOMER:{customer_id}:USDT:WITHDRAWAL_RESERVED",
                            customer_id=customer_id,
                            currency="USDT",
                            debit=10,
                            credit=0,
                        ),
                        LedgerJournalLine(
                            journal_id=journal.id,
                            line_no=2,
                            account_code=f"LIABILITY:CUSTOMER:{customer_id}:USDT:AVAILABLE",
                            customer_id=customer_id,
                            currency="USDT",
                            debit=0,
                            credit=10,
                        ),
                    ]
                )
                db.add(
                    LedgerEntry(
                        customer_id=customer_id,
                        currency="USDT",
                        entry_type="WITHDRAWAL_RELEASE",
                        debit=0,
                        credit=10,
                        amount=10,
                        reference_type="WITHDRAWAL",
                        reference_id=reference_id,
                        idempotency_key=idem,
                        metadata_json="{}",
                    )
                )
                await db.commit()

                # The old ledger only stored the effective release (10), while
                # the immutable withdrawal request preserves the original ask (30).
                result = await release_withdrawal(
                    db, customer_id, 30, reference_id=reference_id
                )
                assert result.customer_id == customer_id
        finally:
            await engine.dispose()

    asyncio.run(run())

