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


def test_legacy_branches_receive_runtime_only_patch_without_overwriting_conflicts() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    paths_start = workflow.index("NATIVE_AI_PATHS=(")
    paths_end = workflow.index(")", paths_start)
    runtime_path_list = workflow[paths_start:paths_end]

    assert "tests/" not in runtime_path_list
    assert '"app/main.py"' in runtime_path_list
    assert '"ENTRY_EXIT_3_9_0.md"' in runtime_path_list
    assert 'NATIVE_AI_BASE_COMMIT="f9c1730c423ff40522366aab5a1bcc960ff1d5ac"' in workflow
    assert 'NATIVE_AI_COMMIT="$GITHUB_SHA"' in workflow
    assert (
        'git diff --binary "$NATIVE_AI_BASE_COMMIT" "$NATIVE_AI_COMMIT" -- "$path"'
        in workflow
    )
    assert 'git apply --3way --index "$RUNNER_TEMP/native-ai-path.patch"' in workflow
    assert 'git checkout "$start_sha" -- "$path"' in workflow
    assert 'git cherry-pick' not in workflow
    assert "apply_native_ai_cleanup_paths" in workflow
    assert "verify_native_ai_impl" in workflow
    assert "provider references remain" in workflow
    assert 'if [[ -z "$before_refs" ]] && verify_native_ai_impl; then' in workflow
    assert "ACTIVE PROVIDER REFERENCES before propagation" in workflow
    assert "ACTIVE PROVIDER REFERENCES REMAIN after per-path cleanup" in workflow
    assert r"generativelanguage\.googleapis\.com" in workflow
    assert r"api\.groq\.com" in workflow
    assert r"api\.cerebras\.ai" in workflow
    assert "AI_GATEWAY_API_KEY|AI_GATEWAY_BASE_URL|AI_GATEWAY_MODEL" in workflow
    assert "CEREBRAS_API_KEY|CEREBRAS_MODEL" in workflow
