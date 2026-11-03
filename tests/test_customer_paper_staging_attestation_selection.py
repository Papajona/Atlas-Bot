from pathlib import Path


def test_customer_paper_staging_selects_latest_manual_exact_commit_before_checking_success():
    workflow = Path(".github/workflows/staging-paper-deploy.yml").read_text(encoding="utf-8")
    production_gate = workflow.split(
        'if [[ "$workflow" == "production-verification.yml" ]]; then', 1
    )[1].split("\n              else", 1)[0]
    selector = production_gate.split("--jq ", 1)[1]

    # The latest manual attestation for this exact main commit is authoritative.
    # A later failed/cancelled/in-progress retry must not be hidden by an older pass.
    assert '.headBranch == "main"' in selector
    assert '.event == "workflow_dispatch"' in selector
    assert '.headSha == ' in selector
    assert '.status == "completed"' not in selector
    assert '.conclusion == "success"' not in selector
    assert selector.index('.headSha == ') < selector.index("sort_by(.databaseId) | last")
    assert "sort_by(.databaseId) | last //" in selector

    # Outcome checks happen only after choosing the newest matching run.
    assert 'if [[ "$run_status" == "completed" ]]; then' in workflow
    assert 'if [[ "$run_conclusion" != "success" ]]; then' in workflow

    attestation_check = workflow.split(
        'verification_json="$(gh run view', 1
    )[1].split("\n                fi", 1)[0]
    assert '.jobs[].steps[] | select(.name == "Atlas Native AI offline verification (no provider calls)") | .conclusion' in attestation_check
    assert '"$native_outcome" != "success"' in attestation_check
    assert 'headSha // ""' in attestation_check
    assert 'headBranch // ""' in attestation_check
    assert 'event // ""' in attestation_check
