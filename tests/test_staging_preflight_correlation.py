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


def test_staging_identity_preflight_is_callable_by_deployment_without_nested_dispatch():
    deploy = (ROOT / ".github/workflows/staging-paper-deploy.yml").read_text()
    preflight = (ROOT / ".github/workflows/staging-identity-preflight.yml").read_text()

    assert "workflow_dispatch:" in preflight
    assert "workflow_call:" in preflight
    assert "request_id:" in preflight
    assert "verify-staging-identity:" in preflight
    assert "gh workflow run staging-identity-preflight.yml" not in deploy
    assert "displayTitle" not in deploy
    job = deploy.split("  verify-staging-identity:", 1)[1].split(
        "  customer-paper-acceptance:", 1
    )[0]
    assert "needs: deploy-paper-staging" in job
    assert "if: needs.deploy-paper-staging.result == 'success'" in job
    assert "uses: ./.github/workflows/staging-identity-preflight.yml" in job
    assert "request_id: atlas-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}" in job


def test_customer_acceptance_is_blocked_when_identity_preflight_does_not_pass():
    deploy = (ROOT / ".github/workflows/staging-paper-deploy.yml").read_text()
    acceptance_job = deploy.split("  customer-paper-acceptance:", 1)[1]
    assert "verify-staging-identity" in acceptance_job
    assert "if: needs.verify-staging-identity.result == 'success'" in acceptance_job
    assert "identity_preflight_passed: ${{ needs.verify-staging-identity.result == 'success' }}" in acceptance_job
    assert "uses: ./.github/workflows/customer-paper-staging-acceptance.yml" in acceptance_job
