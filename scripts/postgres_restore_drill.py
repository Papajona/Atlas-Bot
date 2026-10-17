from __future__ import annotations

import argparse
import asyncio
import json
import os
from decimal import Decimal

from sqlalchemy import select

from app.customer_funds import ledger_invariant_report, post_journal
from app.db import CustomerLedgerAccount, SessionLocal


CUSTOMER_ID = 900001
CURRENCY = "USDT"
AMOUNT = Decimal("123.450000")
IDEM = "restore-drill:customer-900001"


async def seed() -> None:
    async with SessionLocal() as db:
        async with db.begin():
            existing = (
                await db.execute(
                    select(CustomerLedgerAccount).where(
                        CustomerLedgerAccount.customer_id == CUSTOMER_ID,
                        CustomerLedgerAccount.currency == CURRENCY,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise RuntimeError("restore drill seed customer already exists")

            ledger = CustomerLedgerAccount(
                customer_id=CUSTOMER_ID,
                currency=CURRENCY,
                available=AMOUNT,
                trading_reserved=Decimal("0"),
                withdrawal_reserved=Decimal("0"),
            )
            db.add(ledger)
            await db.flush()

            await post_journal(
                db,
                currency=CURRENCY,
                entry_type="RESTORE_DRILL",
                reference_type="TEST",
                reference_id="restore-drill-900001",
                idempotency_key=IDEM,
                lines=[
                    {
                        "account_code": "ASSET:RESTORE_DRILL",
                        "debit": AMOUNT,
                        "credit": Decimal("0"),
                    },
                    {
                        "account_code": f"LIABILITY:CUSTOMER:{CUSTOMER_ID}:{CURRENCY}:AVAILABLE",
                        "customer_id": CUSTOMER_ID,
                        "debit": Decimal("0"),
                        "credit": AMOUNT,
                    },
                ],
                description="Disposable PostgreSQL backup/restore integrity drill",
            )

        report = await ledger_invariant_report(db)
        if not report["ok"]:
            raise RuntimeError(json.dumps(report, sort_keys=True))
        print(json.dumps({"phase": "seed", "invariant": report}, sort_keys=True))


async def verify() -> None:
    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(CustomerLedgerAccount).where(
                    CustomerLedgerAccount.customer_id == CUSTOMER_ID,
                    CustomerLedgerAccount.currency == CURRENCY,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise RuntimeError("restore drill seed ledger row is missing after restore")
        if Decimal(str(row.available)) != AMOUNT:
            raise RuntimeError(
                f"restored available balance mismatch: {row.available!r} != {AMOUNT}"
            )

        report = await ledger_invariant_report(db)
        if not report["ok"]:
            raise RuntimeError(json.dumps(report, sort_keys=True))

        print(
            json.dumps(
                {
                    "phase": "restore_verify",
                    "customer_id": CUSTOMER_ID,
                    "available": str(row.available),
                    "invariant": report,
                },
                sort_keys=True,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("seed", "verify"), required=True)
    args = parser.parse_args()
    if not os.environ.get("DATABASE_URL", "").startswith("postgresql"):
        raise SystemExit("DATABASE_URL must be PostgreSQL")
    asyncio.run(seed() if args.phase == "seed" else verify())


if __name__ == "__main__":
    main()
