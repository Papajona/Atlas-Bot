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
            assert report["reason"] == "NO_VERIFIED_BINANCE_ACCOUNTS"
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



def test_provider_backed_binance_refresh_preserves_exact_decimal_before_persistence(monkeypatch):
    from types import SimpleNamespace
    import app.custody_locations as custody

    class FakeBroker:
        def fetch_balance(self):
            # Binance's raw API represents these values as decimal strings.
            return {"total": {"USDT": 123456789.12345678}, "info": {"balances": [
                {"asset": "USDT", "free": "123456789.12345678", "locked": "0.00000000"}
            ]}}

    def fake_factory(account, **kwargs):
        assert kwargs["sandbox"] is False
        return FakeBroker()

    captured = {}

    async def capture_record(db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(custody, "record_custody_observation", capture_record)

    async def check():
        account = SimpleNamespace(
            customer_id=77, status="VERIFIED", api_key="read-key",
            secret_ref="projects/test/secrets/binance-read/versions/latest",
        )
        row = await custody.refresh_binance_custody_observation(
            None, account, secret_fetcher=lambda _: "not-used-by-fake",
            broker_factory=fake_factory,
        )
        expected = Decimal("123456789.123456780000")
        assert row.balance == expected
        assert captured["balance"] == expected
        assert captured["customer_id"] == 77
        assert captured["provider"] == "binance"
        assert captured["source_reference"].startswith("binance:balance:")

    asyncio.run(check())


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


def test_emergency_kill_bypasses_rate_limiter_when_redis_is_down(monkeypatch):
    from starlette.requests import Request
    from starlette.responses import Response
    import app.main as main

    async def redis_failure(*args, **kwargs):
        raise ConnectionError("Redis unavailable")

    monkeypatch.setattr(main, "allow_rate_limit", redis_failure)
    called = {"next": False}

    async def call_next(request):
        called["next"] = True
        return Response("reached emergency endpoint", status_code=200)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/risk/kill",
        "raw_path": b"/api/risk/kill", "query_string": b"",
        "headers": [], "client": ("127.0.0.1", 12345), "server": ("test", 80),
        "root_path": "",
    }
    response = asyncio.run(main.api_rate_limit(Request(scope), call_next))
    assert response.status_code == 200
    assert called["next"] is True



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
    block = source.split("if live and not signal_timestamp:", 1)[1].split("await db.commit()", 1)[0]
    assert 'raise RiskBlocked("Signal timestamp must include a timezone")' in block
    assert "ts = ts.replace(tzinfo=timezone.utc)" not in block



def test_deficit_threshold_rejects_nonpositive_and_nonfinite_values():
    from pydantic import ValidationError
    from app.config import Settings
    for value in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            Settings(environment="staging", customer_deficit_limit_usdt=value)
