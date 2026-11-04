from pathlib import Path


def test_customer_paper_staging_selects_latest_manual_exact_commit_before_checking_success():
    workflow = Path(".github/workflows/staging-paper-deploy.yml").read_text(encoding="utf-8")
    production_condition = 'if [[ "$workflow" == "production-verification.yml" ]]; then'
    assert workflow.count(production_condition) == 2

    # The first production-specific block selects the latest manual run for the
    # exact main SHA. Outcome filtering must not hide a newer failed retry.
    selector_gate = workflow.split(production_condition, 1)[1].split(
        "\n              else", 1
    )[0]
    selector = selector_gate.split("--jq ", 1)[1]
    assert '.headBranch == "main"' in selector
    assert '.event == "workflow_dispatch"' in selector
    assert '.headSha == ' in selector
    assert '.status == "completed"' not in selector
    assert '.conclusion == "success"' not in selector
    assert selector.index('.headSha == ') < selector.index("sort_by(.databaseId) | last")
    assert "sort_by(.databaseId) | last //" in selector

    # The second production-specific block must enforce the actual manual
    # attestation, including the named Native AI step, inside the production gate.
    attestation_gate = workflow.rsplit(production_condition, 1)[1].split(
        "\n                fi", 1
    )[0]
    assert 'verification_json="$(gh run view "$run_id" --repo "$GITHUB_REPOSITORY" --json headSha,headBranch,event,status,conclusion,jobs)"' in attestation_gate
    assert 'native_outcome="$(jq -r' in attestation_gate
    assert '.jobs[].steps[] | select(.name == "Atlas Native AI offline verification (no provider calls)") | .conclusion' in attestation_gate
    assert '"$native_outcome" != "success"' in attestation_gate
    assert 'headSha // ""' in attestation_gate
    assert 'headBranch // ""' in attestation_gate
    assert 'event // ""' in attestation_gate

    # The general outcome check runs only after the latest matching run was selected.
    assert 'if [[ "$run_status" == "completed" ]]; then' in workflow
    assert 'if [[ "$run_conclusion" != "success" ]]; then' in workflow
