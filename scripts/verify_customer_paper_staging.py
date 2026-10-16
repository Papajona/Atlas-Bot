#!/usr/bin/env python3
"""Read-only preflight for a separately provisioned customer-paper PostgreSQL database.

Does not enable customer paper execution, create schema, post journals, or mutate either DB.
Both DATABASE_URL and CUSTOMER_PAPER_DATABASE_URL must be supplied by the protected staging
environment. A PASS is a schema/privilege preflight only, not an operational isolation drill.
"""
from __future__ import annotations

import asyncio
import json
import sys

from app.customer_paper_ledger import preflight_isolated_customer_paper_ledger


async def audit() -> dict:
    return await preflight_isolated_customer_paper_ledger(require_enabled=False)


def main() -> int:
    try:
        report = asyncio.run(audit())
    except Exception as exc:
        # Avoid printing DSNs, driver errors, or other connection details into CI logs.
        print(json.dumps({
            "status": "BLOCKED",
            "reason": "PAPER_LEDGER_PREFLIGHT_EXCEPTION",
            "error_type": type(exc).__name__,
        }, sort_keys=True))
        return 2
    safe_report = {
        key: report[key]
        for key in ("status", "reason", "detail", "missing_tables", "findings", "privileges", "identity")
        if key in report
    }
    print(json.dumps(safe_report, sort_keys=True, default=str, allow_nan=False))
    return 0 if report.get("status") == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
