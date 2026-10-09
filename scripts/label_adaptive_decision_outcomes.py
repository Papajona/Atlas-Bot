#!/usr/bin/env python3
"""Research-only labeler for recorded adaptive decisions.

Dry-run is the default. Pass --persist to append outcome labels to the encrypted,
hash-chained audit log. This script never trains or promotes a model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.decision_outcomes import DEFAULT_HORIZONS_BARS, process_decision_outcome_labels


def _parse_horizons(value: str) -> tuple[int, ...]:
    try:
        horizons = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("horizons must be comma-separated positive integers") from exc
    if not horizons or any(horizon <= 0 or horizon > 1000 for horizon in horizons):
        raise argparse.ArgumentTypeError("horizons must be comma-separated integers from 1 to 1000")
    return tuple(sorted(set(horizons)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100, help="maximum valid labels to create (default: 100)")
    parser.add_argument(
        "--horizons",
        type=_parse_horizons,
        default=DEFAULT_HORIZONS_BARS,
        help="observed completed bars forward, comma-separated (default: 1,3,6)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="market-data lookback requested from the source adapters (default: 365)",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="append labels to the encrypted audit chain; omitted means dry-run only",
    )
    args = parser.parse_args()
    if args.limit < 1 or args.days < 1:
        parser.error("--limit and --days must be positive")

    result = asyncio.run(
        process_decision_outcome_labels(
            limit=args.limit,
            horizons_bars=args.horizons,
            days=args.days,
            persist=args.persist,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    if result.get("failed"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
