"""Acceptance harness for custody/withdrawal financial simulation.

No blockchain, exchange withdrawal, or signing key is contacted. The test uses
Atlas's real customer ledger services against an isolated in-memory database.
"""
import asyncio
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.customer_funds import (
    customer_balance,
    post_deposit,
    reserve_withdrawal,
    release_withdrawal,
    settle_withdrawal,
)
from app.db import Base, CustomerLedgerAccount, LedgerEntry, LedgerJournal


def test_custody_withdrawal_acceptance_round_trip_is_idempotent():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                db.add(CustomerLedgerAccount(customer_id=9001, available=Decimal("0")))
                await db.commit()

                # 1. Confirmed custody deposit -> authoritative customer ledger.
                await post_deposit(
                    db,
                    customer_id=9001,
                    wallet_id=7001,
                    amount=100,
                    provider_reference="acceptance-deposit-1",
                    metadata={"harness": "custody-withdrawal"},
                    provider="test-custody",
                )
                await db.commit()

                # Duplicate provider delivery must not mint another balance.
                await post_deposit(
                    db,
                    customer_id=9001,
                    wallet_id=7001,
                    amount=100,
                    provider_reference="acceptance-deposit-1",
                    metadata={"harness": "custody-withdrawal", "replay": True},
                    provider="test-custody",
                )
                await db.commit()

                balance = await customer_balance(db, 9001)
                assert balance == {
                    "available": 100.0,
                    "trading_reserved": 0.0,
                    "withdrawal_reserved": 0.0,
                    "total": 100.0,
                }

                # 2. Withdrawal authorization boundary: reserve before release.
                await reserve_withdrawal(db, 9001, 25, reference_id="acceptance-withdrawal-1")
                await db.commit()
                balance = await customer_balance(db, 9001)
                assert balance["available"] == 75.0
                assert balance["withdrawal_reserved"] == 25.0

                # 3. Simulated failed payout releases the exact reserve.
                await release_withdrawal(db, 9001, 25, reference_id="acceptance-withdrawal-release-1")
                await db.commit()
                balance = await customer_balance(db, 9001)
                assert balance["available"] == 100.0
                assert balance["withdrawal_reserved"] == 0.0

                # 4. Re-reserve and simulate a completed payout.
                await reserve_withdrawal(db, 9001, 30, reference_id="acceptance-withdrawal-2")
                await db.commit()
                await settle_withdrawal(db, 9001, 30, reference_id="acceptance-withdrawal-2")
                await db.commit()

                balance = await customer_balance(db, 9001)
                assert balance["available"] == 70.0
                assert balance["withdrawal_reserved"] == 0.0
                assert balance["total"] == 70.0

                # 5. Audit/ledger evidence must exist for deposit, reserve, release,
                # and settlement; idempotent deposit must appear only once.
                entries = (
                    await db.execute(
                        select(LedgerEntry)
                        .where(LedgerEntry.customer_id == 9001)
                        .order_by(LedgerEntry.id)
                    )
                ).scalars().all()
                types = [row.entry_type for row in entries]
                assert types.count("DEPOSIT_CREDIT") == 1
                assert "WITHDRAWAL_RESERVE" in types
                assert "WITHDRAWAL_RELEASE" in types
                assert "WITHDRAWAL_SETTLEMENT" in types

                journals = (
                    await db.execute(
                        select(LedgerJournal)
                        .where(LedgerJournal.currency == "USDT")
                        .order_by(LedgerJournal.id)
                    )
                ).scalars().all()
                idempotency = [j.idempotency_key for j in journals]
                assert len(idempotency) == len(set(idempotency))

        finally:
            await engine.dispose()

    asyncio.run(run())
