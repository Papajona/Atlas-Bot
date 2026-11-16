from __future__ import annotations

import copy

import pytest

from scripts.resolve_staging_dispatch_receipt import (
    CHECK_RUN_NAME,
    resolve_staging_dispatch_receipt,
)


REQUEST_ID = "atlas-43930000000-1-1731780000000000000"
WORKFLOW = ".github/workflows/staging-identity-preflight.yml"
SHA = "a" * 40


def _payload(*, request_id: str = REQUEST_ID, workflow: str = WORKFLOW) -> dict:
    summary = "\n".join(
        (
            f"request_id={request_id}",
            f"workflow_path={workflow}",
            "run_id=43930000123",
            "run_attempt=1",
            f"head_sha={SHA}",
            "head_branch=main",
            "event=workflow_dispatch",
        )
    )
    return {
        "check_runs": [
            {
                "name": CHECK_RUN_NAME,
                "external_id": request_id,
                "head_sha": SHA,
                "status": "in_progress",
                "app": {"slug": "github-actions"},
                "output": {"title": CHECK_RUN_NAME, "summary": summary},
            }
        ]
    }


def test_resolves_only_the_single_exact_nonce_receipt():
    assert (
        resolve_staging_dispatch_receipt(
            _payload(),
            request_id=REQUEST_ID,
            workflow_path=WORKFLOW,
            expected_sha=SHA,
        )
        == "43930000123"
    )


def test_absent_nonce_is_retryable_and_does_not_select_an_unrelated_run():
    with pytest.raises(LookupError):
        resolve_staging_dispatch_receipt(
            {"check_runs": [_payload(request_id="different-nonce")["check_runs"][0]]},
            request_id=REQUEST_ID,
            workflow_path=WORKFLOW,
            expected_sha=SHA,
        )


def test_rejects_duplicate_receipts_for_the_same_nonce():
    payload = _payload()
    payload["check_runs"].append(copy.deepcopy(payload["check_runs"][0]))
    with pytest.raises(ValueError, match="multiple check runs"):
        resolve_staging_dispatch_receipt(
            payload,
            request_id=REQUEST_ID,
            workflow_path=WORKFLOW,
            expected_sha=SHA,
        )


def test_rejects_tampered_or_mismatched_receipt_identity():
    mutations = (
        lambda p: p["check_runs"][0].update(head_sha="b" * 40),
        lambda p: p["check_runs"][0]["app"].update(slug="untrusted-app"),
        lambda p: p["check_runs"][0]["output"].update(title="unrelated check"),
        lambda p: p["check_runs"][0]["output"].update(summary=p["check_runs"][0]["output"]["summary"].replace("head_branch=main", "head_branch=feature")),
        lambda p: p["check_runs"][0]["output"].update(summary=p["check_runs"][0]["output"]["summary"].replace("event=workflow_dispatch", "event=push")),
        lambda p: p["check_runs"][0]["output"].update(summary=p["check_runs"][0]["output"]["summary"].replace("run_id=43930000123", "run_id=0")),
        lambda p: p["check_runs"][0]["output"].update(summary=p["check_runs"][0]["output"]["summary"].replace("run_attempt=1", "run_attempt=0")),
    )
    for mutate in mutations:
        payload = _payload()
        mutate(payload)
        with pytest.raises(ValueError):
            resolve_staging_dispatch_receipt(
                payload,
                request_id=REQUEST_ID,
                workflow_path=WORKFLOW,
                expected_sha=SHA,
            )


def test_rejects_wrong_workflow_and_duplicate_summary_fields():
    with pytest.raises(ValueError, match="workflow_path"):
        resolve_staging_dispatch_receipt(
            _payload(workflow=".github/workflows/customer-paper-staging-acceptance.yml"),
            request_id=REQUEST_ID,
            workflow_path=WORKFLOW,
            expected_sha=SHA,
        )

    payload = _payload()
    payload["check_runs"][0]["output"]["summary"] += "\nevent=workflow_dispatch"
    with pytest.raises(ValueError):
        resolve_staging_dispatch_receipt(
            payload,
            request_id=REQUEST_ID,
            workflow_path=WORKFLOW,
            expected_sha=SHA,
        )


def test_rejects_malformed_api_payload():
    with pytest.raises(ValueError, match="unexpected shape"):
        resolve_staging_dispatch_receipt(
            {"runs": []},
            request_id=REQUEST_ID,
            workflow_path=WORKFLOW,
            expected_sha=SHA,
        )
