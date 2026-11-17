from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_branch_pruning_workflows_share_concurrency_and_require_verified_propagation():
    regular = (ROOT / ".github/workflows/safe-merged-branch-pruning.yml").read_text()
    one_time = (ROOT / ".github/workflows/prune-contained-branches-once.yml").read_text()

    assert "group: atlas-native-ai-branch-propagation-and-pruning" in regular
    assert "group: atlas-native-ai-branch-propagation-and-pruning" in one_time
    assert "cancel-in-progress: false" in regular
    assert "cancel-in-progress: false" in one_time
    assert "\n  workflow_dispatch:\n" in regular
    assert "\n  workflow_run:\n" in regular
    assert "- Propagate Atlas Native AI across branches" in regular
    assert "github.event.workflow_run.head_branch == 'main'" in regular
    assert "github.event.workflow_run.conclusion == 'success'" in regular
    assert "for propagation_status in queued in_progress waiting requested pending action_required stale;" in regular
    assert "atlas-branch-prune-once-20261020" not in regular
    assert "atlas-branch-prune-content-once-20261020" not in regular
    assert "atlas-branch-prune-reviewed-once-20261020" not in regular
    assert "atlas-branch-prune-verified-duplicates-once-20261020" not in regular
    assert "ALREADY ABSENT (concurrent cleanup removed ref)" in one_time


def test_staging_workflow_uses_executable_run_and_current_main_guards():
    import yaml

    deploy = yaml.safe_load(
        (ROOT / ".github/workflows/staging-paper-deploy.yml").read_text(encoding="utf-8")
    )
    preflight = yaml.safe_load(
        (ROOT / ".github/workflows/staging-identity-preflight.yml").read_text(encoding="utf-8")
    )

    preflight_input = preflight["on"]["workflow_dispatch"]["inputs"]["request_id"]
    assert preflight_input["required"] is False

    steps = deploy["jobs"]["deploy-paper-staging"]["steps"]
    by_name = {step.get("name"): step for step in steps if step.get("name")}
    preflight_step = by_name[
        "Dispatch and require read-only staging identity preflight on this exact commit"
    ]["run"]
    acceptance_step = by_name[
        "Dispatch and require customer-paper acceptance on this exact deployment"
    ]["run"]

    # The verifier is executable workflow code; its success/failure behaviour is
    # exercised with matching, stale, malformed, and tampered payloads in the
    # dedicated behavior test module.
    for run_block in (preflight_step, acceptance_step):
        assert "scripts/verify_staging_dispatch.py main-sha" in run_block
        assert "scripts/verify_staging_dispatch.py workflow-run" in run_block
