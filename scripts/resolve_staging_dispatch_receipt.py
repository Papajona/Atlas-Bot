"""Resolve a dispatched staging workflow run from a nonce-bound check-run receipt.

GitHub CLI normally prints the workflow-run URL after a workflow dispatch. Some
successful dispatch responses contain no run details, so the child workflow
publishes a check run whose external_id is the caller's one-time nonce. This
module accepts exactly one matching receipt and validates every identity field;
it never guesses from a list of recent workflow runs.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

CHECK_RUN_NAME = "Atlas staging dispatch correlation"
_REQUIRED_FIELDS = {
    "request_id",
    "workflow_path",
    "run_id",
    "run_attempt",
    "head_sha",
    "head_branch",
    "event",
}


def resolve_staging_dispatch_receipt(
    payload: dict[str, Any],
    *,
    request_id: str,
    workflow_path: str,
    expected_sha: str,
) -> str:
    """Return the exact child run ID or raise on absent/ambiguous/invalid proof.

    LookupError means the receipt has not arrived yet and is safe to retry.
    ValueError means a receipt exists but cannot be trusted and must fail
    closed without selecting another run.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
        raise ValueError("request ID is not a bounded safe nonce")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise ValueError("expected commit SHA is malformed")
    if not workflow_path.startswith(".github/workflows/") or not workflow_path.endswith(".yml"):
        raise ValueError("expected workflow path is malformed")

    if not isinstance(payload, list) or not payload:
        raise ValueError("paginated check-runs API response has an unexpected shape")

    check_runs: list[Any] = []
    expected_total: int | None = None
    for page in payload:
        if not isinstance(page, dict) or not isinstance(page.get("check_runs"), list):
            raise ValueError("a check-runs API page has an unexpected shape")
        total = page.get("total_count")
        if not isinstance(total, int) or total < 0:
            raise ValueError("a check-runs API page has no valid total_count")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise ValueError("check-runs pagination changed total_count between pages")
        check_runs.extend(page["check_runs"])
    if expected_total != len(check_runs):
        raise ValueError("check-runs pagination is incomplete; refusing a partial nonce lookup")

    matches = [
        item
        for item in check_runs
        if isinstance(item, dict)
        and item.get("name") == CHECK_RUN_NAME
        and item.get("external_id") == request_id
    ]
    if not matches:
        raise LookupError("nonce-bound staging receipt has not appeared yet")
    if len(matches) != 1:
        raise ValueError("nonce is attached to multiple check runs; run identity is ambiguous")

    receipt = matches[0]
    if receipt.get("head_sha") != expected_sha:
        raise ValueError("receipt check run is attached to a different commit")
    if (receipt.get("app") or {}).get("slug") != "github-actions":
        raise ValueError("receipt was not created by the GitHub Actions app")
    if receipt.get("status") not in {"in_progress", "completed"}:
        raise ValueError("receipt check run has an unexpected status")

    output = receipt.get("output")
    if not isinstance(output, dict) or output.get("title") != CHECK_RUN_NAME:
        raise ValueError("receipt check-run title is missing or unexpected")
    summary = output.get("summary")
    if not isinstance(summary, str) or not summary:
        raise ValueError("receipt summary is missing")

    fields: dict[str, str] = {}
    for line in summary.splitlines():
        if not line or "=" not in line:
            raise ValueError("receipt summary contains a malformed field")
        key, value = line.split("=", 1)
        if key in fields or key not in _REQUIRED_FIELDS or not value:
            raise ValueError("receipt summary contains duplicate, unknown, or empty fields")
        fields[key] = value
    if set(fields) != _REQUIRED_FIELDS:
        raise ValueError("receipt summary does not contain the complete identity")

    expected = {
        "request_id": request_id,
        "workflow_path": workflow_path,
        "head_sha": expected_sha,
        "head_branch": "main",
        "event": "workflow_dispatch",
    }
    for key, value in expected.items():
        if fields[key] != value:
            raise ValueError(f"receipt {key} does not match the expected dispatch")

    if not re.fullmatch(r"[1-9][0-9]*", fields["run_id"]):
        raise ValueError("receipt run ID is malformed")
    if not re.fullmatch(r"[1-9][0-9]*", fields["run_attempt"]):
        raise ValueError("receipt run attempt is malformed")
    return fields["run_id"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--workflow-path", required=True)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()

    try:
        payload = json.load(sys.stdin)
        run_id = resolve_staging_dispatch_receipt(
            payload,
            request_id=args.request_id,
            workflow_path=args.workflow_path,
            expected_sha=args.expected_sha,
        )
    except LookupError as exc:
        print(f"NOT_FOUND: {exc}", file=sys.stderr)
        return 3
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"BLOCKED: invalid staging dispatch receipt: {exc}", file=sys.stderr)
        return 1

    print(run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
