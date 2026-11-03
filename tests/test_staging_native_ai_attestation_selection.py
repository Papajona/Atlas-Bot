from pathlib import Path


def test_staging_uses_latest_successful_manual_native_ai_attestation_for_exact_commit():
    workflow = Path(".github/workflows/staging-paper-deploy.yml").read_text(encoding="utf-8")
    production_gate = workflow.split(
        'if [[ "$workflow" == "production-verification.yml" ]]; then', 1
    )[1].split("\n              else", 1)[0]
    selector = production_gate.split("--jq ", 1)[1]

    assert '.headBranch == "main"' in selector
    assert '.event == "workflow_dispatch"' in selector
    assert '.headSha == ' in selector
    assert '.status == "completed"' in selector
    assert '.conclusion == "success"' in selector
    assert selector.index('.status == "completed"') < selector.index("sort_by(.databaseId)")
    assert selector.index('.conclusion == "success"') < selector.index("sort_by(.databaseId)")
    assert "sort_by(.databaseId) | last //" in selector
    assert 'select(.name == "Atlas Native AI offline verification (no provider calls)")' in workflow
