"""Regression tests for reusable, exact-SHA customer-paper acceptance gates."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_acceptance_is_a_reusable_workflow_called_only_after_identity_preflight():
    deploy = (ROOT / ".github" / "workflows" / "staging-paper-deploy.yml").read_text(
        encoding="utf-8"
    )
    acceptance = (ROOT / ".github" / "workflows" / "customer-paper-staging-acceptance.yml").read_text(
        encoding="utf-8"
    )

    assert "      - name: Dispatch and require customer-paper acceptance on this exact deployment" not in deploy
    assert "displayTitle" not in deploy
    assert "gh workflow run" not in deploy
    assert "workflow_call:" in acceptance
    job = deploy.split("  customer-paper-acceptance:", 1)[1]
    assert "needs:" in job
    assert "verify-staging-identity" in job
    assert "if: needs.verify-staging-identity.result == 'success'" in job
    assert "uses: ./.github/workflows/customer-paper-staging-acceptance.yml" in job
    assert "target_sha: ${{ github.sha }}" in job
    assert "deployment_run_id: ${{ format('{0}', github.run_id) }}" in job
    assert "deployment_run_attempt: ${{ format('{0}', github.run_attempt) }}" in job


def test_acceptance_workflow_still_validates_exact_deployment_and_parent_preflight():
    workflow = (ROOT / ".github" / "workflows" / "customer-paper-staging-acceptance.yml").read_text(
        encoding="utf-8"
    )

    assert "identity_preflight_passed:" in workflow
    assert 'IDENTITY_PREFLIGHT_PASSED: ${{ inputs.identity_preflight_passed || false }}' in workflow
    assert 'test "$GITHUB_REF" = "refs/heads/main"' in workflow
    assert 'test "$deployment_sha" = "$TARGET_SHA"' in workflow
    assert 'test "$deployment_name" = "Deploy Paper-Only Atlas Staging"' in workflow
    assert 'test "$deployment_attempt_before" = "$DEPLOYMENT_RUN_ATTEMPT"' in workflow
    assert 'test "$deployment_attempt_after" = "$DEPLOYMENT_RUN_ATTEMPT"' in workflow
    assert 'test "$preflight_outcome" = "success"' in workflow
