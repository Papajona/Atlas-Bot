#!/usr/bin/env python3
"""Validate research deployment inputs for Cloud Run.

This checks input presence, delimiter safety, and the production DSR floor only.
It does not prove that the cited fee/calibration evidence is genuine or adequate.
"""
from __future__ import annotations

import math
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print("ERROR: expected source, evidence ID, DSR floor, and delimiter", file=sys.stderr)
        return 2

    source, evidence_id, raw_threshold, delimiter = argv[1:]
    if delimiter not in {",", "@"}:
        print("ERROR: Cloud Run delimiter must be ',' or '@'", file=sys.stderr)
        return 2
    if not source.strip():
        print("ERROR: RESEARCH_FEE_SOURCE must not be empty", file=sys.stderr)
        return 2
    if not evidence_id.strip():
        print("ERROR: RESEARCH_FEE_EVIDENCE_ID must not be empty", file=sys.stderr)
        return 2

    for name, value in (
        ("RESEARCH_FEE_SOURCE", source),
        ("RESEARCH_FEE_EVIDENCE_ID", evidence_id),
        ("RESEARCH_MIN_DEFLATED_SHARPE", raw_threshold),
    ):
        if delimiter in value:
            print(f"ERROR: {name} must not contain the active Cloud Run delimiter", file=sys.stderr)
            return 2

    try:
        threshold = float(raw_threshold)
    except (TypeError, ValueError):
        print("ERROR: RESEARCH_MIN_DEFLATED_SHARPE must be numeric and >= 0.95", file=sys.stderr)
        return 2
    if not math.isfinite(threshold) or threshold < 0.95:
        print("ERROR: RESEARCH_MIN_DEFLATED_SHARPE must be finite and >= 0.95", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
