from pathlib import Path


def test_staging_selects_latest_manual_native_ai_attestation_before_checking_outcome():
    workflow = Path(".github/workflows/staging-paper-deploy.yml").read_text(encoding="utf-8")
    production_gate = workflow.split(
        'if [[ "$workflow" == "production-verification.yml" ]]; then', 1
    )[1].split("\n              else", 1)[0]
    selector = production_gate.split("--jq ", 1)[1]

    # Select the newest manual run for this exact main commit first. Do not
    # filter to successful runs here: a newer failed run must supersede an older pass.
    assert '.headBranch == "main"' in selector
    assert '.event == "workflow_dispatch"' in selector
    assert '.headSha == ' in selector
    assert '.status == "completed"' not in selector
    assert '.conclusion == "success"' not in selector
    assert selector.index(".headSha == ") < selector.index("sort_by(.databaseId) | last")

    # Evaluate the selected run's actual result after selection, failing closed
    # when the latest exact-commit dispatch is failed or otherwise non-successful.
    assert 'if [[ "$run_status" == "completed" ]]; then' in production_gate
    assert 'if [[ "$run_conclusion" != "success" ]]; then' in production_gate
    assert 'BLOCKED: $label completed with conclusion' in production_gate
    assert 'select(.name == "Atlas Native AI offline verification (no provider calls)")' in workflow
