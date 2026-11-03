from pathlib import Path


def test_customer_paper_staging_requires_successful_exact_commit_native_ai_attestation():
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
    assert selector.index('.conclusion == "success"') < selector.index("sort_by(.databaseId)")

    attestation_check = workflow.split(
        'verification_json="$(gh run view', 1
    )[1].split("\n                fi", 1)[0]
    assert '.jobs[].steps[] | select(.name == "Atlas Native AI offline verification (no provider calls)") | .conclusion' in attestation_check
    assert '"$native_outcome" != "success"' in attestation_check
    assert '"headSha // \\"\\""' in attestation_check
    assert '"headBranch // \\"\\""' in attestation_check
    assert '"event // \\"\\""' in attestation_check
