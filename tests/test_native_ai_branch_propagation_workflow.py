"""Regression tests for the repository-wide native-AI propagation workflow."""

from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "propagate-native-ai-all-branches.yml"
)


def test_update_refs_uses_supported_graphql_fields_and_compare_and_swap() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert (
        "refUpdates: [{name: $name, beforeOid: $before, afterOid: $after, force: false}]"
        in workflow
    )
    assert "{ clientMutationId }" in workflow
    assert '-F name="refs/heads/$branch"' in workflow
    assert '-F before="$expected_sha"' in workflow
    assert '-F after="$new_sha"' in workflow
    assert "refId" not in workflow
    assert ".data.updateRefs.ref.target.oid" not in workflow


def test_success_requires_read_back_of_expected_branch_tip() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert '.data.updateRefs.clientMutationId // empty' in workflow
    assert '.data.repository.ref.target.oid // empty' in workflow
    assert '"$read_back_sha" == "$new_sha"' in workflow
    assert '((.errors // []) | length) == 0' in workflow


def test_inconclusive_mutation_never_claims_the_remote_branch_was_unchanged() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "update_rc=2" in workflow
    assert "REMOTE STATE UNRESOLVED for refs/heads/$branch" in workflow
    assert "remote branch state unresolved after update attempt" in workflow
    assert 'echo "UNRESOLVED BRANCH: $failure" >&2' in workflow
    assert 'cat "$RUNNER_TEMP/cherry-pick.log" >&2' in workflow
    assert "existing-ref update rejected; branch left unchanged" not in workflow


def test_legacy_branches_receive_runtime_only_patch_with_hunk_conflicts_favoring_cleanup() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    paths_start = workflow.index("NATIVE_AI_PATHS=(")
    paths_end = workflow.index(")", paths_start)
    runtime_path_list = workflow[paths_start:paths_end]

    assert "tests/" not in runtime_path_list
    assert (
        'git diff --binary "$NATIVE_AI_PARENT" "$NATIVE_AI_COMMIT" -- "${NATIVE_AI_PATHS[@]}"'
        in workflow
    )
    assert 'git cherry-pick -X theirs "$NATIVE_AI_RUNTIME_PATCH_COMMIT"' in workflow
    assert 'git cherry-pick "$NATIVE_AI_COMMIT"' not in workflow
    assert "verify_native_ai_impl" in workflow
    assert "provider references remain" in workflow
