"""Fail-closed validation for staging workflow dispatch evidence.

These functions are shared by the staging workflow and behaviour-level tests.
They deliberately validate the GitHub Actions run payload rather than relying
on a matching display title or a source-text assertion.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from typing import Any


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
_ALLOWED_STATUSES = frozenset(
    {"queued", "in_progress", "waiting", "requested", "pending", "completed"}
)


class StagingDispatchVerificationError(ValueError):
    """Raised when staging dispatch evidence is missing, stale, or inconsistent."""


def verify_current_main_sha(*, actual_sha: str, expected_sha: str) -> None:
    """Require the observed remote main SHA to equal the deployment SHA."""
    if not isinstance(expected_sha, str) or not _SHA_RE.fullmatch(expected_sha):
        raise StagingDispatchVerificationError("expected commit SHA is malformed")
    if not isinstance(actual_sha, str) or not _SHA_RE.fullmatch(actual_sha):
        raise StagingDispatchVerificationError("current main SHA is missing or malformed")
    if actual_sha != expected_sha:
        raise StagingDispatchVerificationError(
            f"main advanced: expected {expected_sha}, observed {actual_sha}"
        )


def verify_dispatched_workflow_run(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_sha: str,
) -> str:
    """Verify the exact dispatched child run and return its validated status.

    In-progress states are valid while the parent polls. A completed child is
    accepted only with a successful conclusion. All identity fields are exact.
    """
    if not isinstance(payload, Mapping):
        raise StagingDispatchVerificationError("workflow run payload is not an object")
    if not isinstance(expected_run_id, str) or not _RUN_ID_RE.fullmatch(expected_run_id):
        raise StagingDispatchVerificationError("expected workflow run ID is malformed")
    if not isinstance(expected_sha, str) or not _SHA_RE.fullmatch(expected_sha):
        raise StagingDispatchVerificationError("expected commit SHA is malformed")

    actual_id = payload.get("databaseId")
    if isinstance(actual_id, bool) or str(actual_id) != expected_run_id:
        raise StagingDispatchVerificationError("workflow run ID does not match this dispatch")

    actual_sha = payload.get("headSha")
    if actual_sha != expected_sha:
        raise StagingDispatchVerificationError("workflow run commit SHA does not match deployment")

    if payload.get("headBranch") != "main":
        raise StagingDispatchVerificationError("workflow run is not on main")
    if payload.get("event") != "workflow_dispatch":
        raise StagingDispatchVerificationError("workflow run was not created by workflow_dispatch")

    status = payload.get("status")
    if not isinstance(status, str) or status not in _ALLOWED_STATUSES:
        raise StagingDispatchVerificationError("workflow run status is missing or unsupported")

    conclusion = payload.get("conclusion")
    if status == "completed" and conclusion != "success":
        raise StagingDispatchVerificationError(
            f"completed workflow run did not succeed (conclusion={conclusion!r})"
        )
    if status != "completed" and conclusion not in (None, ""):
        raise StagingDispatchVerificationError(
            "non-completed workflow run unexpectedly has a conclusion"
        )
    return status


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    main_sha = subparsers.add_parser("main-sha", help="verify the remote main SHA")
    main_sha.add_argument("--expected-sha", required=True)
    main_sha.add_argument("--actual-sha", required=True)

    run = subparsers.add_parser("workflow-run", help="verify a dispatched child workflow run")
    run.add_argument("--expected-run-id", required=True)
    run.add_argument("--expected-sha", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "main-sha":
            verify_current_main_sha(
                actual_sha=args.actual_sha,
                expected_sha=args.expected_sha,
            )
            print("PASS: current main is the exact expected commit.")
            return 0

        payload = json.load(sys.stdin)
        status = verify_dispatched_workflow_run(
            payload,
            expected_run_id=args.expected_run_id,
            expected_sha=args.expected_sha,
        )
        print(f"PASS: dispatched workflow run identity verified (status={status}).")
        return 0
    except (StagingDispatchVerificationError, json.JSONDecodeError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
