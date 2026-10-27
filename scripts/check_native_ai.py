#!/usr/bin/env python3
"""Offline Atlas Native AI smoke check: no network calls, hosted models or API keys."""
from __future__ import annotations
import json
import sys
from pathlib import Path

_REPOSITORY_ROOT = str(Path(__file__).resolve().parents[1])
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)

from app.native_ai import native_ai_health_check


def main() -> int:
    report = native_ai_health_check()
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("status") == "PASS" and report.get("network_calls") == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
