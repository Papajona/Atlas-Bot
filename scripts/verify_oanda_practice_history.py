"""Read-only OANDA practice history contract check.

Run from the repository root with OANDA practice credentials configured:
    python scripts/verify_oanda_practice_history.py --days 365 --symbol EUR_USD

This performs read-only candle requests only; it never submits orders.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.config import settings
from app.data import fetch_forex_oanda


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--symbol", default="EUR_USD")
    parser.add_argument("--timeframe", default="1h", choices=("1m", "5m", "15m", "30m", "1h", "4h", "1d"))
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be at least 1")
    if not settings.oanda_practice:
        raise SystemExit("FAIL: OANDA practice mode must be enabled; live mode is not permitted.")
    if not settings.oanda_account_id or not settings.oanda_api_token:
        raise SystemExit("FAIL: OANDA practice account ID and API token are required.")

    # Guard bounds bracket the fetch call. The production function enforces its own
    # stricter half-open [requested_start, requested_end) interval.
    started = datetime.now(timezone.utc)
    frame = fetch_forex_oanda(args.symbol, timeframe=args.timeframe, days=args.days)
    finished = datetime.now(timezone.utc)
    if frame.empty:
        raise SystemExit("FAIL: OANDA practice endpoint returned no in-window completed candles.")
    timestamps = pd.DatetimeIndex(frame.index)
    if timestamps.tz is None:
        raise SystemExit("FAIL: returned candle timestamps are not timezone-aware.")
    timestamps = timestamps.tz_convert("UTC")
    if not timestamps.is_monotonic_increasing or timestamps.has_duplicates:
        raise SystemExit("FAIL: candle timestamps are unordered or duplicated.")
    if timestamps.min().to_pydatetime() < started - timedelta(days=args.days):
        raise SystemExit("FAIL: returned candle precedes the requested history window.")
    if timestamps.max().to_pydatetime() >= finished:
        raise SystemExit("FAIL: returned candle is at or after the fetch completion time.")

    print(json.dumps({
        "status": "PASS",
        "mode": "oanda_practice_read_only",
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "requested_days": args.days,
        "candles": len(frame),
        "first_candle_utc": timestamps.min().isoformat(),
        "last_candle_utc": timestamps.max().isoformat(),
        "unique_sorted_timestamps": True,
        "orders_submitted": 0,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
