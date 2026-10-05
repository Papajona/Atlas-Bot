#!/usr/bin/env python3
"""List ledger drifts (read-only) or journal one customer's reviewed opening-balance drift.

  python -m scripts.backfill_opening_balance --list
  python -m scripts.backfill_opening_balance --customer-id 42 --expected-amount 50.0 --operator jonathan --note "legacy wallet, reviewed vs wallet table"
  ...same command plus --apply to post it.

Dry-run is the default. Never run --apply for a customer whose drift you have not explained.
"""
import argparse
import asyncio
import json
import sys

from app.db import SessionLocal
from app.ledger_backfill import backfill_opening_balance, compute_bucket_drifts


async def _run(args) -> int:
    async with SessionLocal() as db:
        if args.list:
            drifts = await compute_bucket_drifts(db)
            print(json.dumps({"drift_count": len(drifts), "drifts": drifts}, indent=2))
            return 0 if not drifts else 1
        if args.customer_id is None or args.expected_amount is None:
            print("--customer-id and --expected-amount are required (or use --list)", file=sys.stderr)
            return 2
        try:
            result = await backfill_opening_balance(db, customer_id=args.customer_id, expected_amount=args.expected_amount,
                                                    operator=args.operator, note=args.note, apply=args.apply)
        except ValueError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3
        print(json.dumps(result, indent=2))
        if not args.apply:
            print("DRY RUN - nothing written. Re-run with --apply to post this journal.")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--customer-id", type=int)
    ap.add_argument("--expected-amount", type=str, help="the AVAILABLE drift you reviewed, e.g. 50.0")
    ap.add_argument("--operator", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--apply", action="store_true")
    return asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
