"""Regression tests for the repository-wide native-AI propagation workflow."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "propagate-native-ai-all-branches.yml"
MIGRATION = ROOT / "scripts" / "migrate_legacy_native_ai.py"


def test_update_refs_uses_supported_graphql_fields_and_compare_and_swap() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "refUpdates: [{name: $name, beforeOid: $before, afterOid: $after, force: false}]" in workflow
    assert "{ clientMutationId }" in workflow
    assert '-F name="refs/heads/$branch"' in workflow
    assert '-F before="$expected_sha"' in workflow
    assert '-F after="$new_sha"' in workflow
    assert "refId" not in workflow
    assert ".data.updateRefs.ref.target.oid" not in workflow


def test_success_requires_readback_and_actual_branch_ref_publication() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert '.data.updateRefs.clientMutationId // empty' in workflow
    assert '.data.repository.ref.target.oid // empty' in workflow
    assert '"$read_back_sha" == "$new_sha"' in workflow
    assert '((.errors // []) | length) == 0' in workflow
    assert 'if update_existing_branch_ref "$branch" "$start_sha" "$new_sha"; then' in workflow
    assert 'updated+=("$branch ($start_sha -> $new_sha)")' in workflow
    assert "remote branch state unresolved after update attempt" in workflow


def test_inconclusive_mutation_never_claims_the_remote_branch_was_unchanged() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "update_rc=2" in workflow
    assert "REMOTE STATE UNRESOLVED for refs/heads/$branch" in workflow
    assert 'echo "UNRESOLVED BRANCH: $failure" >&2' in workflow
    assert "existing-ref update rejected; branch left unchanged" not in workflow


def test_legacy_branches_use_semantic_migration_not_bulk_cherry_pick() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    paths_start = workflow.index("NATIVE_AI_PATHS=(")
    paths_end = workflow.index(")", paths_start)
    runtime_path_list = workflow[paths_start:paths_end]

    assert "tests/" not in runtime_path_list
    assert 'git show "$GITHUB_SHA:scripts/migrate_legacy_native_ai.py"' in workflow
    assert 'python "$RUNNER_TEMP/migrate_legacy_native_ai.py" --canonical-commit "$NATIVE_AI_CANONICAL_COMMIT" --cleanup-commit "$NATIVE_AI_CLEANUP_COMMIT" --extra-base-commit "$NATIVE_AI_EXTRA_BASE_COMMIT" --extra-commit "$NATIVE_AI_EXTRA_COMMIT"' in workflow
    assert 'NATIVE_AI_CANONICAL_COMMIT="f8bb968198ec909e66cc04b888716f87ded6c0fa"' in workflow
    assert 'NATIVE_AI_CLEANUP_COMMIT="f5e303dad1a93ce1a33940b3e2b16d118897ebf6"' in workflow
    assert 'NATIVE_AI_EXTRA_COMMIT="2317ccc4c3293e8bbd7043970876eceb509d2d1d"' in workflow
    assert 'extra_parent="$(git rev-parse "$NATIVE_AI_EXTRA_COMMIT^")"' in workflow
    assert 'if [[ "$extra_parent" != "$NATIVE_AI_EXTRA_BASE_COMMIT" ]]; then' in workflow
    assert '"app/main.py"' in runtime_path_list
    assert '"ENTRY_EXIT_3_9_0.md"' in runtime_path_list
    assert '"app/src/main/java/com/atlas/trading/network/ReleaseAdvisorClient.kt"' in runtime_path_list
    assert '"app/src/main/java/com/atlas/trading/ui/ReleaseAdvisorScreen.kt"' in runtime_path_list
    assert "git grep -IlEi" not in workflow
    assert "git grep -nIEi" in workflow
    assert "AI_GATEWAY_API_KEY" in workflow
    assert "SECRET_OPENAI|atlas-openai|OPENAI_[[:alnum:]_]+" in workflow
    assert "openai-compatible" in workflow
    assert "git clean -fd" in workflow
    assert 'git cherry-pick "$NATIVE_AI_COMMIT"' not in workflow
    assert 'git cherry-pick "$NATIVE_AI_RUNTIME_PATCH_COMMIT"' not in workflow
    assert 'git cherry-pick -X theirs' not in workflow
    assert "semantic Native AI migration refused or failed" in workflow
    assert "provider references remain after semantic migration" in workflow
    assert "ACTIVE PROVIDER REFERENCES before semantic migration" in workflow
    assert "ACTIVE PROVIDER REFERENCES REMAIN after semantic migration" in workflow
    assert r"generativelanguage\.googleapis\.com" in workflow
    assert r"api\.groq\.com" in workflow
    assert r"api\.cerebras\.ai" in workflow


def test_provider_scan_filters_only_historical_pruning_proof_markers() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "':!.github/workflows/prune-contained-branches-once.yml'" not in workflow
    assert "git grep -nIEi" in workflow
    assert r"prune-contained-branches-once\.yml:[0-9]+" in workflow
    assert "grep -F .def _check_groq." in workflow


def test_provider_free_branch_skip_requires_native_error_alias_identity() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "facade.AIProviderError is not native.NativeAIError" in workflow
    assert "AIProviderError must be the app.native_ai.NativeAIError compatibility alias" in workflow
    assert 'if [[ -z "$before_refs" ]] && verify_native_ai_impl; then' in workflow


def test_migration_script_preserves_unrelated_branch_logic_and_fails_closed() -> None:
    helper = MIGRATION.read_text(encoding="utf-8")
    assert "transform_config()" in helper
    assert "transform_strategy_ai(canonical_commit)" in helper
    assert "transform_daily_research()" in helper
    assert "transform_release_diagnostics(extra_commit)" in helper
    assert "EXTRA_PATHS" in helper
    assert "--extra-commit" in helper
    assert "refusing lossy rewrite" in helper
    assert "provider references remain in:" in helper
    assert '_git("add", "-A")' in helper
    assert '_git("diff", "--cached", "--check")' in helper
