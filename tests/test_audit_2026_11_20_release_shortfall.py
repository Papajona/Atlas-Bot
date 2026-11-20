"""release_trading must flag, not hide, a release larger than the reserved balance."""
import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.customer_funds import release_trading
from app.db import Base, CustomerLedgerAccount, Incident, LedgerEntry


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")


async def with_database(check):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await check(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


def test_release_larger_than_reserve_releases_only_reserve_and_opens_incident():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("1"), trading_reserved=Decimal("10")))
            await db.commit()
            await release_trading(db, 1, 25, reference_id="shortfall:1")
            await db.commit()
            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.trading_reserved == Decimal("0")
            assert ledger.available == Decimal("11")
            incident = (await db.execute(select(Incident))).scalar_one()
            assert incident.incident_key == "RELEASE_EXCEEDS_RESERVE:shortfall:1:1"
    asyncio.run(with_database(check))


def test_exact_release_opens_no_incident():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("1"), trading_reserved=Decimal("10")))
            await db.commit()
            await release_trading(db, 1, 10, reference_id="exact:1")
            await db.commit()
            assert (await db.execute(select(Incident))).first() is None
    asyncio.run(with_database(check))



def test_one_micro_usdt_release_shortfall_is_not_hidden_by_tolerance():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=2, available=Decimal("1"), trading_reserved=Decimal("10")))
            await db.commit()
            await release_trading(db, 2, 10.000001, reference_id="shortfall:one-micro")
            await db.commit()
            incident = (await db.execute(select(Incident))).scalar_one()
            assert incident.incident_key == "RELEASE_EXCEEDS_RESERVE:shortfall:one-micro:2"
            assert "0.000001" in incident.detail_json
            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.trading_reserved == Decimal("0")
            assert ledger.available == Decimal("11.000000")

    asyncio.run(with_database(check))


def test_release_replay_rejects_different_requested_amount_without_mutating_ledger():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(
                customer_id=3, available=Decimal("1"), trading_reserved=Decimal("10")
            ))
            await db.commit()

            await release_trading(db, 3, 4, reference_id="replay-mismatch:1")
            await db.commit()

            with pytest.raises(ValueError, match="idempotency key already used"):
                await release_trading(db, 3, 5, reference_id="replay-mismatch:1")
            await db.rollback()

            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.trading_reserved == Decimal("6.000000")
            assert ledger.available == Decimal("5.000000")

    asyncio.run(with_database(check))


def test_release_replay_accepts_same_request_and_preserves_shortfall_request_amount():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(
                customer_id=4, available=Decimal("1"), trading_reserved=Decimal("10")
            ))
            await db.commit()

            await release_trading(db, 4, 25, reference_id="replay-shortfall:1")
            await db.commit()

            # Exercise compatibility with rows written before requested amounts
            # were copied into LedgerEntry.metadata_json. The old incident is the
            # only authoritative record of the original over-reserve request.
            legacy_entry = (
                await db.execute(
                    select(LedgerEntry).where(
                        LedgerEntry.idempotency_key == "release:replay-shortfall:1:4"
                    )
                )
            ).scalar_one()
            legacy_entry.metadata_json = "{}"
            await db.commit()

            await release_trading(db, 4, 25, reference_id="replay-shortfall:1")
            await db.commit()

            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.trading_reserved == Decimal("0.000000")
            assert ledger.available == Decimal("11.000000")

    asyncio.run(with_database(check))


@pytest.mark.parametrize("retry_amount", [10, 25])
def test_legacy_shortfall_replay_without_request_evidence_fails_closed_and_alert_survives_rollback(
    retry_amount, monkeypatch, tmp_path
):
    async def check():
        # A file-backed SQLite database gives the incident writer a genuinely
        # independent connection, matching the production transaction boundary.
        database_path = tmp_path / f"legacy-shortfall-{retry_amount}.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr("app.customer_funds.SessionLocal", sessions)

            async with sessions() as db:
                db.add(
                    CustomerLedgerAccount(
                        customer_id=5,
                        available=Decimal("1"),
                        trading_reserved=Decimal("10"),
                    )
                )
                await db.commit()

                reference_id = f"legacy-shortfall:{retry_amount}"
                await release_trading(db, 5, 25, reference_id=reference_id)
                await db.commit()

                # Model a release created before requested amounts and the
                # RELEASE_EXCEEDS_RESERVE incident were persisted.
                entry = (
                    await db.execute(
                        select(LedgerEntry).where(
                            LedgerEntry.idempotency_key
                            == f"release:{reference_id}:5"
                        )
                    )
                ).scalar_one()
                entry.metadata_json = "{}"
                legacy_incident = (
                    await db.execute(
                        select(Incident).where(
                            Incident.incident_key
                            == f"RELEASE_EXCEEDS_RESERVE:{reference_id}:5"
                        )
                    )
                ).scalar_one()
                await db.delete(legacy_incident)
                await db.commit()

                with pytest.raises(ValueError, match="idempotency key already used"):
                    await release_trading(
                        db, 5, retry_amount, reference_id=reference_id
                    )
                # The caller's failed trade transaction is rolled back. The HIGH
                # replay incident must already have committed in its own session.
                await db.rollback()

            async with sessions() as verify_db:
                replay_incident = (
                    await verify_db.execute(
                        select(Incident).where(
                            Incident.incident_key
                            == f"RELEASE_REPLAY_MISMATCH:release:{reference_id}:5"
                        )
                    )
                ).scalar_one()
                assert replay_incident.severity == "HIGH"
                assert replay_incident.status == "OPEN"

                ledger = (
                    await verify_db.execute(
                        select(CustomerLedgerAccount).where(
                            CustomerLedgerAccount.customer_id == 5
                        )
                    )
                ).scalar_one()
                assert ledger.trading_reserved == Decimal("0.000000")
                assert ledger.available == Decimal("11.000000")
        finally:
            await engine.dispose()

    asyncio.run(check())
