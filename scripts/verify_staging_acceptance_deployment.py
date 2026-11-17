"""Verify the exact staging deployment used by customer-paper acceptance."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from typing import Any


_RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ATTEMPT_RE = re.compile(r"^[1-9][0-9]*$")
_DEPLOYMENT_WORKFLOW_NAME = "Deploy Paper-Only Atlas Staging"
_DEPLOYMENT_JOB_NAME = "deploy-paper-staging"
_REQUIRED_STEPS = (
    "Build and push exact commit image to Artifact Registry",
    "Deploy restricted paper-only Cloud Run service",
    "Dispatch and require read-only staging identity preflight on this exact commit",
)


class StagingAcceptanceEvidenceError(ValueError):
    """Raised when the deployment evidence cannot certify paper acceptance."""


def verify_staging_acceptance_deployment(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_attempt: str,
    expected_sha: str,
    attempt_before: str,
    attempt_after: str,
) -> str:
    """Validate deployment identity, attempt stability and required step outcomes.

    An in-progress parent deployment is accepted only after the image, deploy,
    and identity-preflight steps have succeeded. A completed deployment must
    itself have succeeded too.
    """
    if not isinstance(payload, Mapping):
        raise StagingAcceptanceEvidenceError("deployment evidence is not an object")
    if not isinstance(expected_run_id, str) or not _RUN_ID_RE.fullmatch(expected_run_id):
        raise StagingAcceptanceEvidenceError("expected deployment run ID is malformed")
    for label, value in (
        ("expected attempt", expected_attempt),
        ("attempt before", attempt_before),
        ("attempt after", attempt_after),
    ):
        if not isinstance(value, str) or not _ATTEMPT_RE.fullmatch(value):
            raise StagingAcceptanceEvidenceError(f"{label} is missing or malformed")
    if not isinstance(expected_sha, str) or not _SHA_RE.fullmatch(expected_sha):
        raise StagingAcceptanceEvidenceError("expected deployment SHA is malformed")

    if attempt_before != expected_attempt or attempt_after != expected_attempt:
        raise StagingAcceptanceEvidenceError(
            "deployment run attempt changed or does not match the dispatched attempt"
        )

    actual_run_id = payload.get("databaseId")
    if isinstance(actual_run_id, bool) or str(actual_run_id) != expected_run_id:
        raise StagingAcceptanceEvidenceError("deployment run ID does not match the dispatch")
    if payload.get("name") != _DEPLOYMENT_WORKFLOW_NAME:
        raise StagingAcceptanceEvidenceError("deployment workflow identity does not match")
    if payload.get("headSha") != expected_sha:
        raise StagingAcceptanceEvidenceError("deployment SHA does not match acceptance target")
    if payload.get("headBranch") != "main":
        raise StagingAcceptanceEvidenceError("deployment is not on main")

    status = payload.get("status")
    if status not in {"in_progress", "completed"}:
        raise StagingAcceptanceEvidenceError("deployment status is missing or unsupported")
    if status == "completed" and payload.get("conclusion") != "success":
        raise StagingAcceptanceEvidenceError("completed staging deployment did not succeed")
    if status == "in_progress" and payload.get("conclusion") not in (None, ""):
        raise StagingAcceptanceEvidenceError("in-progress deployment unexpectedly has a conclusion")

    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise StagingAcceptanceEvidenceError("deployment jobs are missing")
    matching_jobs = [
        job for job in jobs
        if isinstance(job, Mapping) and job.get("name") == _DEPLOYMENT_JOB_NAME
    ]
    if len(matching_jobs) != 1:
        raise StagingAcceptanceEvidenceError(
            "expected exactly one deploy-paper-staging job in deployment evidence"
        )

    steps = matching_jobs[0].get("steps")
    if not isinstance(steps, list):
        raise StagingAcceptanceEvidenceError("deployment job steps are missing")
    for step_name in _REQUIRED_STEPS:
        matches = [
            step for step in steps
            if isinstance(step, Mapping) and step.get("name") == step_name
        ]
        if len(matches) != 1:
            raise StagingAcceptanceEvidenceError(
                f"expected exactly one required deployment step: {step_name}"
            )
        if matches[0].get("conclusion") != "success":
            raise StagingAcceptanceEvidenceError(
                f"required deployment step did not succeed: {step_name}"
            )

    return status


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-run-id", required=True)
    parser.add_argument("--expected-attempt", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--attempt-before", required=True)
    parser.add_argument("--attempt-after", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        status = verify_staging_acceptance_deployment(
            payload,
            expected_run_id=args.expected_run_id,
            expected_attempt=args.expected_attempt,
            expected_sha=args.expected_sha,
            attempt_before=args.attempt_before,
            attempt_after=args.attempt_after,
        )
        print(
            "PASS: exact paper-only deployment ID, attempt, SHA and required "
            f"image/deploy/preflight steps verified (status={status})."
        )
        return 0
    except (StagingAcceptanceEvidenceError, json.JSONDecodeError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
