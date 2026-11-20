"""release_trading must flag, not hide, a release larger than the reserved balance."""
import asyncio
from decimal import Decimal

from sqlalchemy import select

from app.customer_funds import release_trading
from app.db import CustomerLedgerAccount, Incident
from tests.test_money_safety_fixes import with_database


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
