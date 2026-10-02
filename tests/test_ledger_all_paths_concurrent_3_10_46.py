"""Complete concurrency coverage for all ledger mutation paths.

Tests ALL functions that mutate customer balances:
  - reserve_trading
  - release_trading
  - reserve_withdrawal
  - release_withdrawal
  - settle_withdrawal
  - settle_realized_pnl
  - settle_trading_fee
  - post_deposit
  - get_or_create_ledger (first-use race)

Run when DATABASE_URL or ATLAS_POSTGRES_URL points at PostgreSQL (with alembic upgrade head applied).
"""
import asyncio
import os
import uuid
from decimal import Decimal

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet
    from app.config import settings
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")


def _pg_url():
    for name in ("ATLAS_POSTGRES_URL", "DATABASE_URL"):
        v = os.getenv(name, "").strip()
        if v.startswith("postgresql"):
            return v
    pytest.skip("Set ATLAS_POSTGRES_URL/DATABASE_URL to a migrated PostgreSQL database")


async def _sessions():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    engine = create_async_engine(
        _pg_url(),
        pool_size=50,
        max_overflow=0,
        connect_args={
            "statement_cache_size": 0,
            "server_settings": {"lock_timeout": "20000"},
        },
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _customer(sessions, available):
    from app.db import CustomerProfile, CustomerLedgerAccount
    async with sessions() as db:
        p = CustomerProfile(
            auth_user_id=f"drill-{uuid.uuid4().hex}",
            email="drill@example.invalid",
            display_name="drill",
            status="ACTIVE",
        )
        db.add(p)
        await db.flush()
        db.add(CustomerLedgerAccount(customer_id=p.id, available=Decimal(available)))
        await db.commit()
        return p.id


async def _balances(sessions, cid):
    from sqlalchemy import select
    from app.db import CustomerLedgerAccount, LedgerJournalLine

    async with sessions() as db:
        ledger = (
            await db.execute(
                select(CustomerLedgerAccount).where(
                    CustomerLedgerAccount.customer_id == cid
                )
            )
        ).scalar_one()

        # Select whole journals that touch this customer, then load ALL lines from
        # those journals. Asset/custody counterparties intentionally have customer_id=None,
        # so filtering lines directly by customer_id would falsely report unbalanced journals.
        journal_ids = (
            select(LedgerJournalLine.journal_id)
            .where(LedgerJournalLine.customer_id == cid)
            .distinct()
            .subquery()
        )
        lines = (
            await db.execute(
                select(LedgerJournalLine).where(
                    LedgerJournalLine.journal_id.in_(select(journal_ids.c.journal_id))
                )
            )
        ).scalars().all()
        return ledger, lines


def _assert_balanced(lines):
    assert sum((Decimal(str(l.debit)) for l in lines), Decimal("0")) == sum(
        (Decimal(str(l.credit)) for l in lines), Decimal("0")
    )


@pytest.mark.asyncio
async def test_concurrent_trading_reserves_never_overdraw():
    """Concurrent reserve_trading calls must not overdraw available balance."""
    from app.customer_funds import reserve_trading

    engine, sessions = await _sessions()
    try:
        cid = await _customer(sessions, "1000")

        async def attempt(i):
            async with sessions() as db:
                try:
                    await reserve_trading(db, cid, 150, reference_id=f"trade-{cid}-{i}")
                    await db.commit()
                    return True
                except ValueError:
                    await db.rollback()
                    return False

        results = await asyncio.gather(*[attempt(i) for i in range(15)])
        ledger, lines = await _balances(sessions, cid)

        assert sum(results) <= 6
        assert ledger.available + ledger.trading_reserved <= Decimal("1000")
        _assert_balanced(lines)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_release_trading_idempotent():
    """Concurrent release_trading calls with same reference must only release once."""
    from app.customer_funds import reserve_trading, release_trading

    engine, sessions = await _sessions()
    try:
        cid = await _customer(sessions, "500")

        async with sessions() as db:
            await reserve_trading(db, cid, 100, reference_id=f"trade-{cid}-1")
            await db.commit()

        async def release_attempt():
            async with sessions() as db:
                await release_trading(db, cid, 100, reference_id=f"release-{cid}")
                await db.commit()

        await asyncio.gather(*[release_attempt() for _ in range(20)])
        ledger, _ = await _balances(sessions, cid)
        assert ledger.available == Decimal("500")
        assert ledger.trading_reserved == Decimal("0")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_withdrawal_reserve_and_release_interleaved():
    """Interleaved reserves and releases must not create negative or over-reserved state."""
    from app.customer_funds import reserve_withdrawal, release_withdrawal

    engine, sessions = await _sessions()
    try:
        cid = await _customer(sessions, "200")

        async def reserve_op(i):
            async with sessions() as db:
                try:
                    await reserve_withdrawal(db, cid, 30, reference_id=f"reserve-{cid}-{i}")
                    await db.commit()
                    return ("reserve", True)
                except ValueError:
                    await db.rollback()
                    return ("reserve", False)

        async def release_op(i):
            async with sessions() as db:
                await release_withdrawal(db, cid, 15, reference_id=f"release-{cid}-{i}")
                await db.commit()
                return ("release", True)

        ops = [reserve_op(i) for i in range(10)] + [release_op(i) for i in range(5)]
        await asyncio.gather(*ops)
        ledger, lines = await _balances(sessions, cid)

        assert ledger.available >= 0
        assert ledger.withdrawal_reserved >= 0
        assert ledger.available + ledger.withdrawal_reserved <= Decimal("200")
        _assert_balanced(lines)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_settle_withdrawal_never_exceeds_reserve():
    """settle_withdrawal must never withdraw more than was reserved."""
    from app.customer_funds import reserve_withdrawal, settle_withdrawal

    engine, sessions = await _sessions()
    try:
        cid = await _customer(sessions, "300")

        async with sessions() as db:
            await reserve_withdrawal(db, cid, 100, reference_id=f"reserve-{cid}")
            await db.commit()

        async def settle_attempt(i):
            async with sessions() as db:
                await settle_withdrawal(
                    db, cid, 50, reference_id=f"settle-{cid}-{i}"
                )
                await db.commit()

        await asyncio.gather(*[settle_attempt(i) for i in range(25)])
        ledger, lines = await _balances(sessions, cid)

        assert ledger.withdrawal_reserved == Decimal("0")
        assert ledger.available == Decimal("200")
        _assert_balanced(lines)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_trading_and_withdrawal_reserves_independent():
    """Trading reserves and withdrawal reserves share the same cash ceiling."""
    from app.customer_funds import reserve_trading, reserve_withdrawal

    engine, sessions = await _sessions()
    try:
        cid = await _customer(sessions, "500")

        async def trade_reserve(i):
            async with sessions() as db:
                try:
                    await reserve_trading(db, cid, 50, reference_id=f"trade-{cid}-{i}")
                    await db.commit()
                    return ("trade", True)
                except ValueError:
                    await db.rollback()
                    return ("trade", False)

        async def withdraw_reserve(i):
            async with sessions() as db:
                try:
                    await reserve_withdrawal(
                        db, cid, 50, reference_id=f"withdraw-{cid}-{i}"
                    )
                    await db.commit()
                    return ("withdraw", True)
                except ValueError:
                    await db.rollback()
                    return ("withdraw", False)

        ops = [trade_reserve(i) for i in range(10)] + [withdraw_reserve(i) for i in range(10)]
        await asyncio.gather(*ops)
        ledger, lines = await _balances(sessions, cid)

        assert ledger.available + ledger.trading_reserved + ledger.withdrawal_reserved <= Decimal("500")
        assert ledger.available >= 0
        assert ledger.trading_reserved >= 0
        assert ledger.withdrawal_reserved >= 0
        _assert_balanced(lines)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_fee_settlement_caps_to_available_plus_reserved():
    """settle_trading_fee with strict=False must cap deduction to available + reserved."""
    from app.customer_funds import reserve_trading, settle_trading_fee

    engine, sessions = await _sessions()
    try:
        cid = await _customer(sessions, "100")

        async with sessions() as db:
            await reserve_trading(db, cid, 50, reference_id=f"reserve-{cid}")
            await db.commit()

        async def fee_attempt(i):
            async with sessions() as db:
                await settle_trading_fee(
                    db,
                    customer_id=cid,
                    fee=10,
                    reference_id=f"fee-{cid}-{i}",
                    strict=False,
                )
                await db.commit()

        await asyncio.gather(*[fee_attempt(i) for i in range(30)])
        ledger, lines = await _balances(sessions, cid)

        assert ledger.available == Decimal("0")
        assert ledger.trading_reserved == Decimal("0")
        deficit = sum(
            (Decimal(str(l.debit)) for l in lines
             if l.account_code.startswith("ASSET:RECEIVABLE:CUSTOMER_DEFICIT")),
            Decimal("0"),
        )
        assert deficit == Decimal("200")
        _assert_balanced(lines)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_pnl_and_fee_settlement_combined():
    """Concurrent P&L losses and fee settlements must remain balanced and not overdraft."""
    from app.customer_funds import settle_realized_pnl, settle_trading_fee

    engine, sessions = await _sessions()
    try:
        cid = await _customer(sessions, "200")

        async def loss_op(i):
            async with sessions() as db:
                await settle_realized_pnl(
                    db,
                    customer_id=cid,
                    amount=-25,
                    reference_id=f"loss-{cid}-{i}",
                    strict=False,
                )
                await db.commit()

        async def fee_op(i):
            async with sessions() as db:
                await settle_trading_fee(
                    db,
                    customer_id=cid,
                    fee=10,
                    reference_id=f"fee-{cid}-{i}",
                    strict=False,
                )
                await db.commit()

        await asyncio.gather(
            *([loss_op(i) for i in range(12)] + [fee_op(i) for i in range(12)])
        )
        ledger, lines = await _balances(sessions, cid)

        assert ledger.available == Decimal("0")
        deficit = sum(
            (Decimal(str(l.debit)) for l in lines
             if l.account_code.startswith("ASSET:RECEIVABLE:CUSTOMER_DEFICIT")),
            Decimal("0"),
        )
        assert deficit == Decimal("220")
        _assert_balanced(lines)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_post_deposit_idempotent_and_provider_namespaced():
    """Duplicate delivery is posted once, while different providers with the same reference both credit."""
    from app.customer_funds import post_deposit

    engine, sessions = await _sessions()
    try:
        cid = await _customer(sessions, "0")

        async def deposit_attempt(provider, reference_id, amount):
            async with sessions() as db:
                await post_deposit(
                    db,
                    customer_id=cid,
                    wallet_id=1,
                    amount=amount,
                    provider_reference=reference_id,
                    metadata={"source": provider},
                    provider=provider,
                )
                await db.commit()

        await asyncio.gather(
            *[
                deposit_attempt("tron-usdt", f"dup-{cid}", 10)
                for _ in range(20)
            ]
        )
        ledger, lines = await _balances(sessions, cid)
        assert ledger.available == Decimal("10")
        _assert_balanced(lines)

        await asyncio.gather(
            *[
                deposit_attempt("other-provider", f"dup-{cid}", 7)
                for _ in range(20)
            ]
        )
        ledger, lines = await _balances(sessions, cid)
        assert ledger.available == Decimal("17")
        _assert_balanced(lines)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_get_or_create_ledger_no_duplicate():
    """Multiple concurrent get_or_create_ledger calls must result in exactly one ledger row."""
    from sqlalchemy import select
    from app.db import CustomerProfile, CustomerLedgerAccount
    from app.customer_funds import get_or_create_ledger

    engine, sessions = await _sessions()
    try:
        async with sessions() as db:
            p = CustomerProfile(
                auth_user_id=f"drill-{uuid.uuid4().hex}",
                email="drill@example.invalid",
                display_name="drill",
                status="ACTIVE",
            )
            db.add(p)
            await db.flush()
            cid = p.id
            await db.commit()

        async def create_op():
            async with sessions() as db:
                ledger = await get_or_create_ledger(db, cid, "USDT")
                await db.commit()
                return ledger.id

        ledger_ids = await asyncio.gather(*[create_op() for _ in range(50)])

        async with sessions() as db:
            rows = (
                await db.execute(
                    select(CustomerLedgerAccount).where(
                        CustomerLedgerAccount.customer_id == cid,
                        CustomerLedgerAccount.currency == "USDT",
                    )
                )
            ).scalars().all()
            assert len(rows) == 1
            assert len(set(ledger_ids)) == 1
            assert ledger_ids[0] == rows[0].id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_idempotency_across_all_functions():
    """All ledger functions with idempotency keys must post exactly once under duplicate calls."""
    from app.customer_funds import (
        reserve_withdrawal,
        settle_trading_fee,
        settle_realized_pnl,
    )

    engine, sessions = await _sessions()
    try:
        cid = await _customer(sessions, "500")

        async def duplicate_reserves():
            async def attempt():
                async with sessions() as db:
                    await reserve_withdrawal(
                        db, cid, 100, reference_id=f"dup-reserve-{cid}"
                    )
                    await db.commit()

            await asyncio.gather(*[attempt() for _ in range(20)])

        await duplicate_reserves()
        ledger, _ = await _balances(sessions, cid)
        assert ledger.withdrawal_reserved == Decimal("100")
        assert ledger.available == Decimal("400")

        async def duplicate_fees():
            async def attempt():
                async with sessions() as db:
                    await settle_trading_fee(
                        db,
                        customer_id=cid,
                        fee=5,
                        reference_id=f"dup-fee-{cid}",
                        strict=False,
                    )
                    await db.commit()

            await asyncio.gather(*[attempt() for _ in range(20)])

        await duplicate_fees()
        ledger, _ = await _balances(sessions, cid)
        assert ledger.available == Decimal("395")

        async def duplicate_losses():
            async def attempt():
                async with sessions() as db:
                    await settle_realized_pnl(
                        db,
                        customer_id=cid,
                        amount=-20,
                        reference_id=f"dup-loss-{cid}",
                        strict=False,
                    )
                    await db.commit()

            await asyncio.gather(*[attempt() for _ in range(20)])

        await duplicate_losses()
        ledger, _ = await _balances(sessions, cid)
        assert ledger.available == Decimal("375")
    finally:
        await engine.dispose()
