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

    preflight_on = preflight.get("on", preflight.get(True))
    preflight_input = preflight_on["workflow_dispatch"]["inputs"]["request_id"]
    assert preflight_input["required"] is False

    steps = deploy["jobs"]["deploy-paper-staging"]["steps"]
    by_name = {step.get("name"): step for step in steps if step.get("name")}
    preflight_step = by_name[
        "Dispatch and require read-only staging identity preflight on this exact commit"
    ]["run"]
    assert '-f "request_id=$request_id"' in preflight_step
