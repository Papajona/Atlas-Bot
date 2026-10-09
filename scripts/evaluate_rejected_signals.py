#!/usr/bin/env python3
"""Generate a read-only rejected-signal outcome and chronological holdout report."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from app.decision_outcome_research import (
    evaluate_decision_outcome_labels,
    load_decision_outcome_labels,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10_000, help="most recent outcome labels to read")
    parser.add_argument("--holdout-fraction", type=float, default=0.30, help="chronological holdout fraction, 0.1 to 0.5")
    parser.add_argument("--min-group-samples", type=int, default=30, help="minimum n for a group to be flagged sample-sufficient")
    parser.add_argument("--output", type=Path, help="optional path to write the JSON report")
    args = parser.parse_args()
    if args.limit < 1 or args.min_group_samples < 1:
        parser.error("--limit and --min-group-samples must be positive")

    labels = asyncio.run(load_decision_outcome_labels(limit=args.limit))
    report = evaluate_decision_outcome_labels(
        labels,
        holdout_fraction=args.holdout_fraction,
        min_group_samples=args.min_group_samples,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
