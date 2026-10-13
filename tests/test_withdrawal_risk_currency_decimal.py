from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base, Withdrawal, WithdrawalDestination
from app.withdrawal_risk import score_withdrawal
from app.withdrawal_security import destination_fingerprint


def _run(coro):
    return asyncio.run(coro)


def test_withdrawal_risk_velocity_is_scoped_to_currency_and_network(monkeypatch):
    async def run():
        monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
        monkeypatch.setattr(settings, "app_encryption_keys_json", "{}")
        monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")
        monkeypatch.setattr(settings, "withdrawal_velocity_24h", 100.0)
        monkeypatch.setattr(settings, "withdrawal_velocity_7d", 200.0)
        monkeypatch.setattr(settings, "withdrawal_balance_ratio_review", 0.50)
        monkeypatch.setattr(settings, "withdrawal_balance_ratio_block", 0.90)
        monkeypatch.setattr(settings, "withdrawal_max_amount", 5000.0)
        monkeypatch.setattr(settings, "withdrawal_max_unknown_24h", 3)

        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            destination = "T" + "A" * 33
            async with sessions() as db:
                db.add_all([
                    Withdrawal(
                        customer_id=7, request_id="usdt-tron", account_ref="7",
                        amount=Decimal("90.10"), currency="USDT", network="TRON",
                        destination=destination, status="PENDING", created_at=now,
                    ),
                    Withdrawal(
                        customer_id=7, request_id="usdt-eth", account_ref="7",
                        amount=Decimal("900.00"), currency="USDT", network="ERC20",
                        destination=destination, status="UNKNOWN", created_at=now,
                    ),
                    Withdrawal(
                        customer_id=7, request_id="btc-tron", account_ref="7",
                        amount=Decimal("800.00"), currency="BTC", network="TRON",
                        destination=destination, status="UNKNOWN", created_at=now,
                    ),
                    WithdrawalDestination(
                        customer_id=7,
                        fingerprint=destination_fingerprint(destination, "USDT", "TRON"),
                        currency="USDT",
                        network="TRON",
                        status="VERIFIED",
                        first_seen_at=now,
                    ),
                ])
                await db.commit()
                result = await score_withdrawal(
                    db,
                    customer_id=7,
                    wallet_balance=Decimal("1000"),
                    amount=Decimal("9.90"),
                    destination=destination,
                    currency="USDT",
                    network="TRON",
                )
            assert Decimal(result["velocity_24h"]) == Decimal("90.10")
            assert Decimal(result["velocity_7d"]) == Decimal("90.10")
            assert result["known_destination"] is True
            assert "24H_VELOCITY_LIMIT" not in result["flags"]
            assert "7D_VELOCITY_LIMIT" not in result["flags"]
            assert "RECENT_UNKNOWN_PAYOUTS" not in result["flags"]
        finally:
            await engine.dispose()

    _run(run())


def test_withdrawal_risk_destination_fingerprint_is_asset_and_network_specific(monkeypatch):
    async def run():
        monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
        monkeypatch.setattr(settings, "app_encryption_keys_json", "{}")
        monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")
        monkeypatch.setattr(settings, "withdrawal_velocity_24h", 10000.0)
        monkeypatch.setattr(settings, "withdrawal_velocity_7d", 25000.0)
        monkeypatch.setattr(settings, "withdrawal_balance_ratio_review", 0.50)
        monkeypatch.setattr(settings, "withdrawal_balance_ratio_block", 0.90)
        monkeypatch.setattr(settings, "withdrawal_max_amount", 5000.0)
        monkeypatch.setattr(settings, "withdrawal_max_unknown_24h", 3)

        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            destination = "T" + "B" * 33
            async with sessions() as db:
                db.add(WithdrawalDestination(
                    customer_id=7,
                    fingerprint=destination_fingerprint(destination, "USDT", "ERC20"),
                    currency="USDT",
                    network="ERC20",
                    status="VERIFIED",
                    first_seen_at=now,
                ))
                await db.commit()
                result = await score_withdrawal(
                    db,
                    customer_id=7,
                    wallet_balance=Decimal("1000"),
                    amount=Decimal("1"),
                    destination=destination,
                    currency="USDT",
                    network="TRON",
                )
            assert result["known_destination"] is False
            assert "NEW_DESTINATION" in result["flags"]
        finally:
            await engine.dispose()

    _run(run())
