"""Regression tests for unified custody observations and transfer safety."""
import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.custody_locations import (
    create_custody_transfer,
    custody_gate,
    fresh_external_assets,
    flag_stale_custody_transfers,
    location_for_binance,
    record_custody_observation,
    transition_custody_transfer,
)
from app.db import Base, CustodyTransfer, utcnow


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


def test_binance_location_is_customer_scoped():
    assert location_for_binance(42) == "EXCHANGE:BINANCE:CUSTOMER:42"
    with pytest.raises(ValueError):
        location_for_binance(0)


def test_observation_upsert_and_decimal_asset_sum():
    async def check(sessions):
        async with sessions() as db:
            await record_custody_observation(
                db, location=location_for_binance(1), provider="binance",
                currency="USDT", balance=Decimal("100.25"), source_reference="balance:1"
            )
            await record_custody_observation(
                db, location=location_for_binance(2), provider="binance",
                currency="USDT", balance=Decimal("50.75"), source_reference="balance:2"
            )
            await db.commit()
        async with sessions() as db:
            report = await fresh_external_assets(db)
            assert report["assets"] == Decimal("151.00")
            assert report["observation_count"] == 2
            assert report["fresh_observation_available"] is True
    asyncio.run(with_database(check))


def test_stale_exchange_observation_is_excluded():
    async def check(sessions):
        async with sessions() as db:
            await record_custody_observation(
                db, location=location_for_binance(1), provider="binance",
                currency="USDT", balance=100, source_reference="old",
                observed_at=utcnow() - timedelta(seconds=settings.custody_observation_max_age_seconds + 1)
            )
            await db.commit()
        async with sessions() as db:
            report = await fresh_external_assets(db)
            assert report["assets"] == Decimal("0")
            assert report["fresh_observation_available"] is False
    asyncio.run(with_database(check))


def test_enabled_custody_gate_fails_closed_without_fresh_observation(monkeypatch):
    monkeypatch.setattr(settings, "binance_custody_reconciliation_enabled", True)
    async def check(sessions):
        async with sessions() as db:
            report = await custody_gate(db, customer_liabilities=100)
            assert report["status"] == "UNKNOWN"
            assert report["reason"] == "NO_FRESH_EXTERNAL_CUSTODY_OBSERVATION"
    asyncio.run(with_database(check))


def test_custody_transfer_is_idempotent_and_state_machine_is_strict():
    async def check(sessions):
        async with sessions() as db:
            transfer = await create_custody_transfer(
                db, customer_id=7, currency="USDT", amount=25,
                source_location="CUSTODY:TRON",
                destination_location=location_for_binance(7),
                idempotency_key="transfer:7:1",
            )
            replay = await create_custody_transfer(
                db, customer_id=7, currency="USDT", amount=25,
                source_location="CUSTODY:TRON",
                destination_location=location_for_binance(7),
                idempotency_key="transfer:7:1",
            )
            assert replay.id == transfer.id
            await transition_custody_transfer(db, transfer.id, status="SUBMITTED", provider_reference="provider-1")
            await transition_custody_transfer(db, transfer.id, status="CONFIRMED", provider_reference="provider-1")
            with pytest.raises(ValueError, match="invalid custody transfer transition"):
                await transition_custody_transfer(db, transfer.id, status="SUBMITTED")
            await db.commit()
        async with sessions() as db:
            row = (await db.execute(select(CustodyTransfer))).scalar_one()
            assert row.status == "CONFIRMED"
            assert row.confirmed_at is not None
    asyncio.run(with_database(check))


def test_in_flight_transfer_does_not_count_as_exchange_asset():
    async def check(sessions):
        async with sessions() as db:
            await create_custody_transfer(
                db, customer_id=9, currency="USDT", amount=100,
                source_location="CUSTODY:TRON",
                destination_location=location_for_binance(9),
                idempotency_key="transfer:9:1",
            )
            await db.commit()
        async with sessions() as db:
            report = await fresh_external_assets(db)
            assert report["assets"] == Decimal("0")
    asyncio.run(with_database(check))


def test_stale_custody_transfer_requires_reconciliation_without_release():
    async def check(sessions):
        old = utcnow() - timedelta(hours=25)
        async with sessions() as db:
            requested = await create_custody_transfer(
                db, customer_id=11, currency="USDT", amount=75,
                source_location="CUSTODY:TRON",
                destination_location=location_for_binance(11),
                idempotency_key="transfer:11:stale-requested",
            )
            submitted = await create_custody_transfer(
                db, customer_id=12, currency="USDT", amount=80,
                source_location="CUSTODY:TRON",
                destination_location=location_for_binance(12),
                idempotency_key="transfer:12:stale-submitted",
            )
            requested.requested_at = old
            await transition_custody_transfer(
                db, submitted.id, status="SUBMITTED", provider_reference="provider-pending"
            )
            submitted.submitted_at = old
            await db.commit()

        async with sessions() as db:
            changed = await flag_stale_custody_transfers(db, timeout_hours=24)
            assert changed == 2
            await db.commit()

        async with sessions() as db:
            rows = (await db.execute(
                select(CustodyTransfer).where(CustodyTransfer.id.in_([requested.id, submitted.id]))
            )).scalars().all()
            assert {row.status for row in rows} == {"RECONCILIATION_REQUIRED"}
            for row in rows:
                detail = __import__("json").loads(row.detail_json)
                assert detail["timeout_review"]["action"] == "RECONCILIATION_REQUIRED_NO_AUTOMATIC_RELEASE"
            # A timeout must not manufacture provider confirmation or erase provider evidence.
            submitted_row = next(row for row in rows if row.id == submitted.id)
            assert submitted_row.provider_reference == "provider-pending"
            assert submitted_row.confirmed_at is None
    asyncio.run(with_database(check))



def test_provider_backed_binance_refresh_records_exact_decimal_balance():
    from types import SimpleNamespace
    from app.custody_locations import refresh_binance_custody_observation
    from app.db import CustodyAssetObservation

    class FakeBroker:
        def fetch_balance(self):
            return {"total": {"USDT": "123456789.12345678"}}

    def fake_factory(account, **kwargs):
        assert kwargs["sandbox"] is False
        return FakeBroker()

    async def check(sessions):
        account = SimpleNamespace(
            customer_id=77, status="VERIFIED", api_key="read-key",
            secret_ref="projects/test/secrets/binance-read/versions/latest",
        )
        async with sessions() as db:
            row = await refresh_binance_custody_observation(
                db, account, secret_fetcher=lambda _: "not-used-by-fake",
                broker_factory=fake_factory,
            )
            await db.commit()
            assert row.balance == Decimal("123456789.123456780000")
            assert row.customer_id == 77
            assert row.provider == "binance"
            assert row.source_reference.startswith("binance:balance:")
        async with sessions() as db:
            saved = (await db.execute(select(CustodyAssetObservation))).scalar_one()
            assert saved.balance == Decimal("123456789.123456780000")
            report = await fresh_external_assets(db)
            assert report["assets"] == Decimal("123456789.123456780000")
    asyncio.run(with_database(check))


def test_provider_refresh_failure_does_not_fabricate_or_refresh_observation():
    from types import SimpleNamespace
    from app.custody_locations import refresh_binance_custody_observation

    class FailedBroker:
        def fetch_balance(self):
            raise TimeoutError("provider unavailable")

    def failed_factory(account, **kwargs):
        return FailedBroker()

    async def check(sessions):
        account = SimpleNamespace(
            customer_id=78, status="VERIFIED", api_key="read-key",
            secret_ref="projects/test/secrets/binance-read/versions/latest",
        )
        async with sessions() as db:
            with pytest.raises(TimeoutError, match="provider unavailable"):
                await refresh_binance_custody_observation(
                    db, account, broker_factory=failed_factory,
                )
            await db.commit()
            assert (await db.execute(select(__import__("app.db", fromlist=["CustodyAssetObservation"]).CustodyAssetObservation))).scalars().all() == []
    asyncio.run(with_database(check))


def test_custody_decimal_schema_matches_existing_migration():
    from pathlib import Path
    from app.db import CustodyAssetObservation, CustodyTransfer
    assert str(CustodyAssetObservation.__table__.c.balance.type) == "NUMERIC(38, 18)"
    assert str(CustodyTransfer.__table__.c.amount.type) == "NUMERIC(38, 18)"
    migration = (Path(__file__).parents[1] / "alembic" / "versions" / "0022_unified_custody.py").read_text()
    assert 'sa.Column("balance", sa.Numeric(38, 18)' in migration
    assert 'sa.Column("amount", sa.Numeric(38, 18)' in migration


def test_emergency_kill_route_is_exempt_from_redis_rate_limiter():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "app" / "main.py").read_text()
    middleware = source.split("async def api_rate_limit", 1)[1].split("@app.middleware", 1)[0]
    assert '"/api/risk/kill"' in middleware



def test_custody_gate_refreshes_provider_and_fails_unknown_on_provider_error(monkeypatch):
    import app.custody_locations as custody

    monkeypatch.setattr(settings, "binance_custody_reconciliation_enabled", True)
    calls = {"count": 0}

    async def refresh_ok(db):
        calls["count"] += 1
        await record_custody_observation(
            db, location=location_for_binance(90), provider="binance", currency="USDT",
            balance=Decimal("150"), source_reference="provider-test",
        )
        return 1

    async def refresh_failed(db):
        calls["count"] += 1
        raise TimeoutError("provider offline")

    async def check(sessions):
        async with sessions() as db:
            monkeypatch.setattr(custody, "refresh_verified_binance_custody_observations", refresh_ok)
            report = await custody.custody_gate(db, customer_liabilities=Decimal("100"))
            assert report["status"] == "OK"
            assert report["provider_accounts_refreshed"] == 1
            await db.commit()
        async with sessions() as db:
            monkeypatch.setattr(custody, "refresh_verified_binance_custody_observations", refresh_failed)
            report = await custody.custody_gate(db, customer_liabilities=Decimal("100"))
            assert report["status"] == "UNKNOWN"
            assert report["reason"] == "BINANCE_PROVIDER_REFRESH_FAILED"
            assert report["exchange_assets"] is None
            assert calls["count"] == 2
    asyncio.run(with_database(check))


def test_naive_live_signal_timestamp_is_rejected_without_timezone_guessing():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "app" / "execution.py").read_text()
    block = source.split("if live:", 1)[1].split("await db.commit()", 1)[0]
    assert 'raise RiskBlocked("Signal timestamp must include a timezone")' in block
    assert "ts = ts.replace(tzinfo=timezone.utc)" not in block
