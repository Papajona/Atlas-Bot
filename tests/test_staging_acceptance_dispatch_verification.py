"""Behaviour-level checks for staging main-SHA and child-run guards."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.verify_staging_dispatch import (
    StagingDispatchVerificationError,
    verify_current_main_sha,
    verify_dispatched_workflow_run,
)


ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
OTHER_SHA = "b" * 40
RUN_ID = "43930000123"


def _run(**overrides):
    payload = {
        "databaseId": int(RUN_ID),
        "headSha": SHA,
        "headBranch": "main",
        "event": "workflow_dispatch",
        "status": "in_progress",
        "conclusion": None,
    }
    payload.update(overrides)
    return payload


def test_accepts_only_the_dispatched_run_with_exact_identity():
    assert (
        verify_dispatched_workflow_run(
            _run(),
            expected_run_id=RUN_ID,
            expected_sha=SHA,
        )
        == "in_progress"
    )


def test_accepts_completed_run_only_when_conclusion_is_success():
    assert (
        verify_dispatched_workflow_run(
            _run(status="completed", conclusion="success"),
            expected_run_id=RUN_ID,
            expected_sha=SHA,
        )
        == "completed"
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"databaseId": 43930000124}, "run ID"),
        ({"headSha": OTHER_SHA}, "commit SHA"),
        ({"headBranch": "feature/untrusted"}, "not on main"),
        ({"event": "push"}, "workflow_dispatch"),
    ],
)
def test_rejects_wrong_run_id_sha_branch_or_event(overrides, message):
    with pytest.raises(StagingDispatchVerificationError, match=message):
        verify_dispatched_workflow_run(
            _run(**overrides),
            expected_run_id=RUN_ID,
            expected_sha=SHA,
        )


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [
        ("completed", "failure"),
        ("completed", "cancelled"),
        ("completed", "timed_out"),
        ("completed", None),
        ("in_progress", "success"),
        ("unknown", None),
        (None, None),
    ],
)
def test_rejects_failed_or_unsupported_run_states(status, conclusion):
    with pytest.raises(StagingDispatchVerificationError):
        verify_dispatched_workflow_run(
            _run(status=status, conclusion=conclusion),
            expected_run_id=RUN_ID,
            expected_sha=SHA,
        )


def test_rejects_malformed_or_missing_run_payload():
    for payload in (None, [], {}, {"databaseId": RUN_ID}):
        with pytest.raises(StagingDispatchVerificationError):
            verify_dispatched_workflow_run(
                payload,
                expected_run_id=RUN_ID,
                expected_sha=SHA,
            )


def test_rejects_malformed_expected_run_identity():
    for run_id, sha in (("0", SHA), ("not-a-run", SHA), (RUN_ID, "bad-sha")):
        with pytest.raises(StagingDispatchVerificationError):
            verify_dispatched_workflow_run(
                _run(),
                expected_run_id=run_id,
                expected_sha=sha,
            )


def test_current_main_guard_accepts_exact_sha_and_rejects_stale_or_invalid_sha():
    verify_current_main_sha(actual_sha=SHA, expected_sha=SHA)
    for actual in (OTHER_SHA, "", "not-a-sha"):
        with pytest.raises(StagingDispatchVerificationError):
            verify_current_main_sha(actual_sha=actual, expected_sha=SHA)


def test_cli_fails_closed_for_a_tampered_child_run():
    script = ROOT / "scripts" / "verify_staging_dispatch.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "workflow-run",
            "--expected-run-id",
            RUN_ID,
            "--expected-sha",
            SHA,
        ],
        input=json.dumps(_run(event="push")),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "BLOCKED:" in result.stderr


def _workflow_steps(path: str) -> list[dict]:
    workflow = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    return workflow["jobs"]["deploy-paper-staging"]["steps"]


def _step(steps: list[dict], name: str) -> dict:
    matches = [step for step in steps if step.get("name") == name]
    assert len(matches) == 1, f"expected one executable workflow step named {name!r}"
    assert isinstance(matches[0].get("run"), str) and matches[0]["run"].strip()
    return matches[0]


def _guard_command(step: str, subcommand: str, *, start_at: int = 0) -> tuple[int, str]:
    marker = f"if ! python3 scripts/verify_staging_dispatch.py {subcommand}"
    matches = list(re.finditer(re.escape(marker), step[start_at:]))
    assert matches, f"missing executable {subcommand} verifier invocation"
    start = start_at + matches[0].start()
    end = step.index("then", start)
    return start, step[start:end]


def _assert_rejects_before_success(step: str, command_start: int, success_marker: str) -> None:
    success = step.index(success_marker)
    assert command_start < success
    failure_branch = step[step.index("then", command_start) : step.index("fi", step.index("then", command_start))]
    assert "exit 1" in failure_branch


def test_deployment_workflow_executes_the_same_guards_covered_by_these_tests():
    steps = _workflow_steps(".github/workflows/staging-paper-deploy.yml")
    preflight = _step(
        steps,
        "Dispatch and require read-only staging identity preflight on this exact commit",
    )["run"]
    acceptance = _step(
        steps,
        "Dispatch and require customer-paper acceptance on this exact deployment",
    )["run"]
    deployment = _step(steps, "Deploy restricted paper-only Cloud Run service")["run"]

    # Verify the deploy guard's exact arguments and that rejection occurs before
    # image selection and the Cloud Run mutation.
    deploy_guard_pos, deploy_guard = _guard_command(deployment, "main-sha")
    assert '--expected-sha "$GITHUB_SHA"' in deploy_guard
    assert '--actual-sha "$current_main_sha"' in deploy_guard
    deploy_failure_end = deployment.index("fi", deployment.index("then", deploy_guard_pos))
    image_pos = deployment.index('IMAGE="$(cat "$RUNNER_TEMP/atlas-paper-staging-image")')
    mutation_pos = deployment.index('gcloud run deploy "$STAGING_SERVICE"')
    assert deploy_guard_pos < deploy_failure_end < image_pos < mutation_pos
    assert "exit 1" in deployment[deployment.index("then", deploy_guard_pos):deploy_failure_end]

    # Preflight: validate the exact child run, then re-read and compare main before
    # the parent emits its success result.
    preflight_run_pos, preflight_run_guard = _guard_command(preflight, "workflow-run")
    assert '--expected-run-id "$preflight_run_id"' in preflight_run_guard
    assert '--expected-sha "$GITHUB_SHA"' in preflight_run_guard
    assert '<<< "$run_json"' in preflight_run_guard
    preflight_success = preflight.index(
        "PASS: read-only staging identity and isolated-ledger preflight run"
    )
    assert preflight_run_pos < preflight_success
    preflight_main_pos, preflight_main_guard = _guard_command(
        preflight, "main-sha", start_at=preflight_run_pos
    )
    assert '--expected-sha "$GITHUB_SHA"' in preflight_main_guard
    assert '--actual-sha "$current_main_sha"' in preflight_main_guard
    _assert_rejects_before_success(
        preflight,
        preflight_main_pos,
        "PASS: read-only staging identity and isolated-ledger preflight run",
    )

    # Acceptance: do not dispatch if main is stale; validate the exact dispatched
    # child ID/SHA and fail before reporting successful acceptance.
    acceptance_main_pos, acceptance_main_guard = _guard_command(acceptance, "main-sha")
    assert '--expected-sha "$GITHUB_SHA"' in acceptance_main_guard
    assert '--actual-sha "$current_main_sha"' in acceptance_main_guard
    dispatch_pos = acceptance.index(
        "gh workflow run customer-paper-staging-acceptance.yml"
    )
    assert acceptance_main_pos < dispatch_pos
    acceptance_run_pos, acceptance_run_guard = _guard_command(acceptance, "workflow-run")
    assert '--expected-run-id "$acceptance_run_id"' in acceptance_run_guard
    assert '--expected-sha "$GITHUB_SHA"' in acceptance_run_guard
    assert '<<< "$run_json"' in acceptance_run_guard
    _assert_rejects_before_success(
        acceptance,
        acceptance_run_pos,
        "PASS: customer-paper acceptance run",
    )
