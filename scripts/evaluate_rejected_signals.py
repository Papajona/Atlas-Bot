#!/usr/bin/env python3
"""Generate a read-only rejected-signal outcome and chronological holdout report."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.decision_outcome_research import (
    DecisionOutcomeHistoryIntegrityError,
    DecisionOutcomeHistoryResourceLimit,
    evaluate_decision_outcome_labels,
    load_decision_outcome_labels,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label-version", help="include only this exact label methodology version")
    parser.add_argument(
        "--limit", type=int,
        help="optional most-recent row limit within --label-version; omitted loads complete bounded history",
    )
    parser.add_argument("--holdout-fraction", type=float, default=0.30, help="chronological holdout fraction, 0.1 to 0.5")
    parser.add_argument("--min-group-samples", type=int, default=30, help="minimum n for a group to be flagged sample-sufficient")
    parser.add_argument("--output", type=Path, help="optional path to write the JSON report")
    args = parser.parse_args()
    if args.min_group_samples < 1:
        parser.error("--min-group-samples must be positive")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.limit is not None and not args.label_version:
        parser.error("--limit requires --label-version so older methodology rows cannot be hidden")
    try:
        labels = asyncio.run(load_decision_outcome_labels(
            label_version=args.label_version,
            limit=args.limit,
        ))
    except (DecisionOutcomeHistoryIntegrityError, DecisionOutcomeHistoryResourceLimit, ValueError) as exc:
        print(f"Unable to load a complete/version-scoped label history: {exc}", file=sys.stderr)
        return 2
    report = evaluate_decision_outcome_labels(
        labels,
        holdout_fraction=args.holdout_fraction,
        min_group_samples=args.min_group_samples,
    )
    coverage = report.get("coverage") if isinstance(report.get("coverage"), dict) else {}
    report["history_selection"] = {
        "requested_label_version": args.label_version or "ALL_VERSIONS",
        "requested_limit_within_version": args.limit,
        "loaded_records": len(labels),
        "loaded_label_version_counts": coverage.get("label_version_counts", {}),
        "loaded_market_timestamp_ranges_by_version": coverage.get("label_version_date_ranges", {}),
    }
    rendered = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
