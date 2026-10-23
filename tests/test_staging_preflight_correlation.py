from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_branch_pruning_workflows_share_concurrency_and_skip_duplicate_marker_run():
    regular = (ROOT / ".github/workflows/safe-merged-branch-pruning.yml").read_text()
    one_time = (ROOT / ".github/workflows/prune-contained-branches-once.yml").read_text()

    assert "group: atlas-branch-pruning" in regular
    assert "group: atlas-branch-pruning" in one_time
    assert "cancel-in-progress: false" in regular
    assert "cancel-in-progress: false" in one_time
    assert "atlas-branch-prune-once-20261020" in regular
    assert "atlas-branch-prune-content-once-20261020" in regular
    assert "atlas-branch-prune-reviewed-once-20261020" in regular
    assert "atlas-branch-prune-verified-duplicates-once-20261020" in regular
    assert "ALREADY ABSENT (concurrent cleanup removed ref)" in one_time


def test_staging_preflight_run_is_uniquely_correlated_and_rechecked_before_success():
    deploy = (ROOT / ".github/workflows/staging-paper-deploy.yml").read_text()
    preflight = (ROOT / ".github/workflows/staging-identity-preflight.yml").read_text()

    assert "inputs.request_id || 'manual'" in preflight
    assert "request_id:" in preflight
    assert '-f "request_id=$request_id"' in deploy
    assert 'gh run view "$preflight_run_id"' in deploy
    assert "run_name" in deploy
    assert "select(.displayTitle == $expected)" in deploy
    tracked_poll = deploy.index('gh run view "$preflight_run_id"')
    final_sha_check = deploy.index('if [[ "$current_main_sha" != "$GITHUB_SHA" ]]; then', tracked_poll)
    success_message = deploy.index("PASS: read-only staging identity and isolated-ledger preflight run", tracked_poll)
    assert final_sha_check < success_message
    assert "gh run list" not in deploy[tracked_poll:success_message]
