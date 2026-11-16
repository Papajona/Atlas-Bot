"""Regression tests for exact-SHA staging acceptance dispatch correlation.

The dispatch response supplies the exact workflow-run URL. The verifier must
poll only that run and reject it if its SHA, branch, or event is unexpected.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _acceptance_dispatch_block() -> str:
    workflow = (ROOT / ".github" / "workflows" / "staging-paper-deploy.yml").read_text(
        encoding="utf-8"
    )
    marker = "      - name: Dispatch and require customer-paper acceptance on this exact deployment"
    assert marker in workflow
    return workflow.split(marker, 1)[1]


def test_acceptance_dispatch_binds_to_run_url_returned_by_gh():
    block = _acceptance_dispatch_block()

    assert 'acceptance_dispatch_output="$(gh workflow run customer-paper-staging-acceptance.yml' in block
    assert 'acceptance_run_url="$(grep -Eo "https://github\\\\.com/${GITHUB_REPOSITORY}/actions/runs/[0-9]+"' in block
    assert 'acceptance_run_id="${acceptance_run_url##*/}"' in block
    assert "gh_retry run list" not in block
    assert "select(.displayTitle == $expected)" not in block


def test_discovered_acceptance_run_is_still_rejected_when_sha_or_branch_differs():
    block = _acceptance_dispatch_block()

    assert 'candidate_sha="$(jq -r \'.headSha // ""\' <<< "$run_json")"' in block
    assert 'candidate_branch="$(jq -r \'.headBranch // ""\' <<< "$run_json")"' in block
    assert 'if [[ "$candidate_sha" != "$GITHUB_SHA" || "$candidate_branch" != "main" ]]; then' in block
    assert "BLOCKED: uniquely correlated acceptance run is not on exact main commit" in block
    assert '|| "$run_event" != "workflow_dispatch" ]]' in block
