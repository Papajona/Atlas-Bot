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
from scripts.verify_staging_acceptance_deployment import (
    StagingAcceptanceEvidenceError,
    verify_staging_acceptance_deployment,
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
    _assert_rejects_before_success(
        preflight,
        preflight_run_pos,
        "PASS: read-only staging identity and isolated-ledger preflight run",
    )

    # The completed-run branch must contain a fresh remote-main read and its
    # exact-SHA verifier. A check performed before the child-run wait is stale
    # by construction and must not satisfy this assertion.
    completed_branch_pos = preflight.index(
        'if [[ "$run_status" == "completed" ]]; then', preflight_run_pos
    )
    main_read_pos = preflight.index(
        'current_main_sha="$(gh_retry api "/repos/$GITHUB_REPOSITORY/git/ref/heads/main"',
        completed_branch_pos,
    )
    preflight_main_pos, preflight_main_guard = _guard_command(
        preflight, "main-sha", start_at=main_read_pos
    )
    assert '--expected-sha "$GITHUB_SHA"' in preflight_main_guard
    assert '--actual-sha "$current_main_sha"' in preflight_main_guard
    assert completed_branch_pos < main_read_pos < preflight_main_pos < preflight_success
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
    _assert_rejects_before_success(
        acceptance,
        acceptance_main_pos,
        "gh workflow run customer-paper-staging-acceptance.yml",
    )
    acceptance_run_pos, acceptance_run_guard = _guard_command(acceptance, "workflow-run")
    assert '--expected-run-id "$acceptance_run_id"' in acceptance_run_guard
    assert '--expected-sha "$GITHUB_SHA"' in acceptance_run_guard
    assert '<<< "$run_json"' in acceptance_run_guard
    _assert_rejects_before_success(
        acceptance,
        acceptance_run_pos,
        "PASS: customer-paper acceptance run",
    )


ACCEPTANCE_REQUIRED_STEPS = (
    "Build and push exact commit image to Artifact Registry",
    "Deploy restricted paper-only Cloud Run service",
    "Dispatch and require read-only staging identity preflight on this exact commit",
)


def _deployment_payload(**overrides):
    steps = [{"name": name, "conclusion": "success"} for name in ACCEPTANCE_REQUIRED_STEPS]
    payload = {
        "databaseId": 43930000999,
        "name": "Deploy Paper-Only Atlas Staging",
        "headSha": SHA,
        "headBranch": "main",
        "status": "in_progress",
        "conclusion": None,
        "jobs": [{"name": "deploy-paper-staging", "steps": steps}],
    }
    payload.update(overrides)
    return payload


def _verify_deployment(payload, *, before="3", after="3"):
    return verify_staging_acceptance_deployment(
        payload,
        expected_run_id="43930000999",
        expected_attempt="3",
        expected_sha=SHA,
        attempt_before=before,
        attempt_after=after,
    )


def test_manual_acceptance_accepts_exact_in_progress_deployment_after_preflight():
    assert _verify_deployment(_deployment_payload()) == "in_progress"


def test_manual_acceptance_accepts_exact_completed_successful_deployment():
    payload = _deployment_payload(status="completed", conclusion="success")
    assert _verify_deployment(payload) == "completed"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"databaseId": 43930000123}, "run ID"),
        ({"name": "Unrelated workflow"}, "workflow identity"),
        ({"headSha": OTHER_SHA}, "SHA"),
        ({"headBranch": "feature/test"}, "not on main"),
        ({"status": "queued"}, "status"),
        ({"status": "completed", "conclusion": "failure"}, "did not succeed"),
    ],
)
def test_manual_acceptance_rejects_wrong_deployment_identity_or_state(overrides, message):
    with pytest.raises(StagingAcceptanceEvidenceError, match=message):
        _verify_deployment(_deployment_payload(**overrides))


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("2", "3"),
        ("3", "4"),
        ("", "3"),
        ("3", "invalid"),
    ],
)
def test_manual_acceptance_rejects_deployment_attempt_drift(before, after):
    with pytest.raises(StagingAcceptanceEvidenceError, match="attempt"):
        _verify_deployment(_deployment_payload(), before=before, after=after)


@pytest.mark.parametrize("required_step", ACCEPTANCE_REQUIRED_STEPS)
def test_manual_acceptance_rejects_missing_or_failed_required_deployment_step(required_step):
    missing = _deployment_payload()
    missing["jobs"][0]["steps"] = [
        step for step in missing["jobs"][0]["steps"] if step["name"] != required_step
    ]
    with pytest.raises(StagingAcceptanceEvidenceError, match="expected exactly one"):
        _verify_deployment(missing)

    failed = _deployment_payload()
    for step in failed["jobs"][0]["steps"]:
        if step["name"] == required_step:
            step["conclusion"] = "failure"
    with pytest.raises(StagingAcceptanceEvidenceError, match="did not succeed"):
        _verify_deployment(failed)


def test_manual_acceptance_rejects_ambiguous_job_or_duplicate_required_step():
    duplicate_job = _deployment_payload()
    duplicate_job["jobs"].append(duplicate_job["jobs"][0])
    with pytest.raises(StagingAcceptanceEvidenceError, match="exactly one deploy-paper-staging"):
        _verify_deployment(duplicate_job)

    duplicate_step = _deployment_payload()
    duplicate_step["jobs"][0]["steps"].append(
        dict(duplicate_step["jobs"][0]["steps"][0])
    )
    with pytest.raises(StagingAcceptanceEvidenceError, match="expected exactly one"):
        _verify_deployment(duplicate_step)


def test_manual_acceptance_workflow_executes_the_tested_deployment_evidence_verifier():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/customer-paper-staging-acceptance.yml").read_text(
            encoding="utf-8"
        )
    )
    on = workflow.get("on", workflow.get(True))
    inputs = on["workflow_dispatch"]["inputs"]
    for field in ("target_sha", "deployment_run_id", "deployment_run_attempt", "dispatch_id"):
        assert inputs[field]["required"] is True

    steps = workflow["jobs"]["customer-paper-acceptance"]["steps"]
    checkout_indices = [
        i for i, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert len(checkout_indices) == 2
    dispatch_checkout_index = next(
        i for i, step in enumerate(steps)
        if step.get("name") == "Checkout dispatch revision for deployment evidence verifier"
    )
    target_checkout_index = next(
        i for i, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/checkout@")
        and step.get("with", {}).get("ref") == "${{ inputs.target_sha }}"
    )
    python_index = next(
        i for i, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/setup-python@")
    )
    validation_indices = [
        i for i, step in enumerate(steps)
        if step.get("name") == "Validate exact successful main deployment"
    ]
    assert len(validation_indices) == 1
    validation_index = validation_indices[0]
    target_identity_index = next(
        i for i, step in enumerate(steps)
        if step.get("name") == "Require checkout to match the exact deployed or manually dispatched commit"
    )
    assert dispatch_checkout_index < python_index < validation_index < target_checkout_index < target_identity_index
    run = steps[validation_index]["run"]
    assert "scripts/verify_staging_acceptance_deployment.py" in run
    assert '--expected-run-id "$DEPLOYMENT_RUN_ID"' in run
    assert '--expected-attempt "$DEPLOYMENT_RUN_ATTEMPT"' in run
    assert '--expected-sha "$TARGET_SHA"' in run
    assert '--attempt-before "$deployment_attempt_before"' in run
    assert '--attempt-after "$deployment_attempt_after"' in run
    assert '<<< "$deployment_json"' in run


def test_parent_rechecks_main_after_acceptance_child_completes_before_success():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/staging-paper-deploy.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["deploy-paper-staging"]["steps"]
    matches = [
        step for step in steps
        if step.get("name") == "Dispatch and require customer-paper acceptance on this exact deployment"
    ]
    assert len(matches) == 1
    run = matches[0]["run"]

    completed = run.index('if [[ "$run_status" == "completed" ]]; then')
    fresh_main = run.index(
        'current_main_sha="$(gh_retry api "/repos/$GITHUB_REPOSITORY/git/ref/heads/main"',
        completed,
    )
    main_guard = run.index(
        "scripts/verify_staging_dispatch.py main-sha",
        fresh_main,
    )
    success = run.index("PASS: customer-paper acceptance run", main_guard)
    failure_branch_end = run.index("fi", run.index("then", main_guard))
    assert completed < fresh_main < main_guard < success
    assert "exit 1" in run[run.index("then", main_guard):failure_branch_end]


def test_superseded_parent_run_cannot_start_deploy_preflight_or_acceptance():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/staging-paper-deploy.yml").read_text(encoding="utf-8")
    )
    jobs = workflow["jobs"]
    assert set(jobs) == {"deploy-paper-staging"}
    steps = jobs["deploy-paper-staging"]["steps"]
    named = {step.get("name"): step for step in steps if step.get("name")}

    gate = named["Wait for required CI gates to pass on this exact commit"]
    assert gate["id"] == "wait-required-gates"
    assert "stale=true" in gate["run"]
    for name in (
        "Validate staging inputs",
        "Checkout exact main commit",
        "Authenticate to Google Cloud with main-only workload identity",
        "Set up Google Cloud SDK",
        "Build and push exact commit image to Artifact Registry",
        "Deploy restricted paper-only Cloud Run service",
        "Dispatch and require read-only staging identity preflight on this exact commit",
        "Dispatch and require customer-paper acceptance on this exact deployment",
    ):
        assert name in named
        condition = named[name].get("if", "")
        assert "steps.wait-required-gates.outputs.stale != 'true'" in condition, name
