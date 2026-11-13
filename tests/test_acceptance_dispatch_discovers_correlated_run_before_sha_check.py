"""Regression tests for exact-SHA staging acceptance dispatch discovery.

The dispatch is correlated by a unique run name containing the deployment SHA,
run ID, attempt, and nonce. Discovery must not filter by the pre-dispatch SHA:
if main advances during dispatch, the correlated run must be found and rejected
immediately by its explicit head-SHA check rather than hidden until timeout.
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


def test_acceptance_run_discovery_uses_unique_correlation_without_sha_filter():
    block = _acceptance_dispatch_block()
    discovery = block.split('run_list="$(gh run list', 1)[1].split(')"', 1)[0]

    assert '--workflow "customer-paper-staging-acceptance.yml"' in discovery
    assert "--event workflow_dispatch" in discovery
    assert "--json databaseId,headSha,headBranch,displayTitle" in discovery
    assert '--commit "$GITHUB_SHA"' not in discovery
    assert 'select(.displayTitle == $expected)' in block


def test_discovered_acceptance_run_is_still_rejected_when_sha_or_branch_differs():
    block = _acceptance_dispatch_block()

    assert 'candidate_sha="$(jq -r \'.headSha // ""\' <<< "$run_json")"' in block
    assert 'candidate_branch="$(jq -r \'.headBranch // ""\' <<< "$run_json")"' in block
    assert 'if [[ "$candidate_sha" != "$GITHUB_SHA" || "$candidate_branch" != "main" ]]; then' in block
    assert "BLOCKED: uniquely correlated acceptance run is not on exact main commit" in block
    assert '|| "$run_name" != "$expected_run_name" ]]' in block
