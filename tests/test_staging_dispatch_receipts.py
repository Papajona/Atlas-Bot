from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest

from scripts.resolve_staging_dispatch_receipt import (
    CHECK_RUN_NAME,
    RetryableSnapshotError,
    resolve_staging_dispatch_receipt,
)


ROOT = Path(__file__).resolve().parents[1]
REQUEST_ID = "atlas-43930000000-1-1731780000000000000"
WORKFLOW = ".github/workflows/staging-identity-preflight.yml"
SHA = "a" * 40


def _payload(*, request_id: str = REQUEST_ID, workflow: str = WORKFLOW) -> list[dict]:
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
    return [
        {
            "total_count": 1,
            "check_runs": [
                {
                    "name": CHECK_RUN_NAME,
                    "external_id": request_id,
                    "head_sha": SHA,
                    "status": "in_progress",
                    "app": {"slug": "github-actions"},
                    "output": {"title": CHECK_RUN_NAME, "summary": summary},
                }
            ],
        }
    ]


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
            _payload(request_id="different-nonce"),
            request_id=REQUEST_ID,
            workflow_path=WORKFLOW,
            expected_sha=SHA,
        )


def test_rejects_duplicate_receipts_for_the_same_nonce():
    payload = _payload()
    payload[0]["check_runs"].append(copy.deepcopy(payload[0]["check_runs"][0]))
    payload[0]["total_count"] = 2
    with pytest.raises(ValueError, match="multiple check runs"):
        resolve_staging_dispatch_receipt(
            payload,
            request_id=REQUEST_ID,
            workflow_path=WORKFLOW,
            expected_sha=SHA,
        )


def test_rejects_tampered_or_mismatched_receipt_identity():
    mutations = (
        lambda p: p[0]["check_runs"][0].update(head_sha="b" * 40),
        lambda p: p[0]["check_runs"][0]["app"].update(slug="untrusted-app"),
        lambda p: p[0]["check_runs"][0]["output"].update(title="unrelated check"),
        lambda p: p[0]["check_runs"][0]["output"].update(
            summary=p[0]["check_runs"][0]["output"]["summary"].replace(
                "head_branch=main", "head_branch=feature"
            )
        ),
        lambda p: p[0]["check_runs"][0]["output"].update(
            summary=p[0]["check_runs"][0]["output"]["summary"].replace(
                "event=workflow_dispatch", "event=push"
            )
        ),
        lambda p: p[0]["check_runs"][0]["output"].update(
            summary=p[0]["check_runs"][0]["output"]["summary"].replace(
                "run_id=43930000123", "run_id=0"
            )
        ),
        lambda p: p[0]["check_runs"][0]["output"].update(
            summary=p[0]["check_runs"][0]["output"]["summary"].replace(
                "run_attempt=1", "run_attempt=0"
            )
        ),
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
    payload[0]["check_runs"][0]["output"]["summary"] += "\nevent=workflow_dispatch"
    with pytest.raises(ValueError):
        resolve_staging_dispatch_receipt(
            payload,
            request_id=REQUEST_ID,
            workflow_path=WORKFLOW,
            expected_sha=SHA,
        )


def test_resolves_a_receipt_on_a_later_check_run_page():
    later_page = _payload()[0]["check_runs"][0]
    payload = [
        {"total_count": 2, "check_runs": [{"name": "other check", "external_id": "other"}]},
        {"total_count": 2, "check_runs": [later_page]},
    ]
    assert (
        resolve_staging_dispatch_receipt(
            payload,
            request_id=REQUEST_ID,
            workflow_path=WORKFLOW,
            expected_sha=SHA,
        )
        == "43930000123"
    )


def test_rejects_duplicate_receipts_split_across_pages():
    receipt = _payload()[0]["check_runs"][0]
    payload = [
        {"total_count": 2, "check_runs": [copy.deepcopy(receipt)]},
        {"total_count": 2, "check_runs": [copy.deepcopy(receipt)]},
    ]
    with pytest.raises(ValueError, match="multiple check runs"):
        resolve_staging_dispatch_receipt(
            payload,
            request_id=REQUEST_ID,
            workflow_path=WORKFLOW,
            expected_sha=SHA,
        )


def test_rejects_partial_paginated_api_results():
    receipt = _payload()[0]["check_runs"][0]
    with pytest.raises(RetryableSnapshotError, match="snapshot is incomplete"):
        resolve_staging_dispatch_receipt(
            [{"total_count": 2, "check_runs": [receipt]}],
            request_id=REQUEST_ID,
            workflow_path=WORKFLOW,
            expected_sha=SHA,
        )



def test_total_count_change_during_pagination_is_retryable():
    receipt = _payload()[0]["check_runs"][0]
    payload = [
        {"total_count": 1, "check_runs": []},
        {"total_count": 2, "check_runs": [receipt]},
    ]
    with pytest.raises(RetryableSnapshotError, match="total_count changed"):
        resolve_staging_dispatch_receipt(
            payload,
            request_id=REQUEST_ID,
            workflow_path=WORKFLOW,
            expected_sha=SHA,
        )


def test_rejects_malformed_api_payload():
    with pytest.raises(ValueError, match="unexpected shape"):
        resolve_staging_dispatch_receipt(
            {"check_runs": []},
            request_id=REQUEST_ID,
            workflow_path=WORKFLOW,
            expected_sha=SHA,
        )


def _workflow_step(workflow: str, name: str) -> str:
    marker = f"      - name: {name}"
    assert workflow.count(marker) == 1, f"expected exactly one workflow step named {name!r}"
    start = workflow.index(marker)
    end = workflow.find("\n      - name:", start + len(marker))
    return workflow[start:] if end < 0 else workflow[start:end]


def _run_script(step: str) -> str:
    marker = "        run: |\n"
    assert marker in step, "expected an executable run block"
    lines = step.split(marker, 1)[1].splitlines()
    code = []
    for line in lines:
        if line.startswith("          "):
            code.append(line[10:])
        elif not line.strip():
            code.append("")
        else:
            break
    assert code, "workflow step has an empty run block"
    return "\n".join(code) + "\n"


def _assert_bash_parses(step: str) -> None:
    result = subprocess.run(
        ["bash", "-n"],
        input=_run_script(step),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_no_url_dispatch_fallback_is_nonce_bound_for_both_child_workflows():
    parent = (ROOT / ".github/workflows/staging-paper-deploy.yml").read_text(encoding="utf-8")
    assert "  checks: read" in parent
    wait_step = _workflow_step(parent, "Wait for required CI gates to pass on this exact commit")
    assert "resolve_staging_dispatch_run_id()" in wait_step
    assert "--paginate --slurp" in wait_step
    assert "rc != 3 && rc != 4" in wait_step
    assert "check-runs?check_name=Atlas%20staging%20dispatch%20correlation" in wait_step
    assert "scripts/resolve_staging_dispatch_receipt.py" in wait_step
    assert "2.87.0" in wait_step and "sort -V" in wait_step

    preflight_dispatch = _workflow_step(
        parent, "Dispatch and require read-only staging identity preflight on this exact commit"
    )
    assert 'resolve_staging_dispatch_run_id "$request_id" ".github/workflows/staging-identity-preflight.yml" "$GITHUB_SHA" 900' in preflight_dispatch
    acceptance_dispatch = _workflow_step(
        parent, "Dispatch and require customer-paper acceptance on this exact deployment"
    )
    assert 'resolve_staging_dispatch_run_id "$dispatch_id" ".github/workflows/customer-paper-staging-acceptance.yml" "$GITHUB_SHA" 5400' in acceptance_dispatch
    assert "gh run list" not in preflight_dispatch + acceptance_dispatch


def test_child_workflows_publish_and_finalize_their_actual_nonce_receipt_steps():
    cases = (
        (
            ".github/workflows/staging-identity-preflight.yml",
            "Publish nonce-bound workflow-run correlation receipt",
            "Finalize nonce-bound workflow-run correlation receipt",
        ),
        (
            ".github/workflows/customer-paper-staging-acceptance.yml",
            "Publish nonce-bound workflow-run correlation receipt",
            "Finalize nonce-bound workflow-run correlation receipt",
        ),
    )
    for relative_path, publish_name, finalize_name in cases:
        workflow = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "  checks: write" in workflow
        publish = _workflow_step(workflow, publish_name)
        finalize = _workflow_step(workflow, finalize_name)
        for field in (
            'external_id="$REQUEST_ID"',
            "run_id=$GITHUB_RUN_ID",
            "run_attempt=$GITHUB_RUN_ATTEMPT",
            "head_sha=$GITHUB_SHA",
            "head_branch=$GITHUB_REF_NAME",
            "event=$GITHUB_EVENT_NAME",
        ):
            assert field in publish
        assert "gh api --method POST" in publish
        assert "gh api --method PATCH" in finalize
        assert "status=completed" in finalize
        _assert_bash_parses(publish)
        _assert_bash_parses(finalize)


def test_dispatch_workflow_shell_helpers_parse_before_the_runner_executes_them():
    parent = (ROOT / ".github/workflows/staging-paper-deploy.yml").read_text(encoding="utf-8")
    wait_step = _workflow_step(parent, "Wait for required CI gates to pass on this exact commit")
    _assert_bash_parses(wait_step)


def test_generated_dispatch_helper_is_valid_shell():
    parent = (ROOT / ".github/workflows/staging-paper-deploy.yml").read_text(encoding="utf-8")
    wait_step = _workflow_step(parent, "Wait for required CI gates to pass on this exact commit")
    outer_script = _run_script(wait_step)
    marker = """cat > "$RUNNER_TEMP/atlas-gh-retry.sh" <<'ATLAS_GH_RETRY'\n"""
    assert outer_script.count(marker) == 1
    helper_script = outer_script.split(marker, 1)[1].split("\nATLAS_GH_RETRY", 1)[0] + "\n"
    result = subprocess.run(
        ["bash", "-n"],
        input=helper_script,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
