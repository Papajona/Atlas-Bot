"""Regression tests for the repository-wide native-AI propagation workflow."""

import ast
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "propagate-native-ai-all-branches.yml"
MIGRATION = ROOT / "scripts" / "migrate_legacy_native_ai.py"


def test_propagation_requires_a_separate_credential_for_workflow_file_updates() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    permissions = workflow.split("permissions:", 1)[1].split("concurrency:", 1)[0]
    upload = workflow.split("update_existing_branch_ref()", 1)[1].split(
        "declare -a updated=", 1
    )[0]

    # workflows is not a GITHUB_TOKEN permission. Branches that need workflow-file
    # changes are only updated when a separately scoped credential is provided.
    assert "contents: write" in permissions
    assert "workflows: write" not in permissions
    assert "ATLAS_WORKFLOW_UPDATE_TOKEN" in workflow
    assert 'git diff --name-only "$expected_sha" "$new_sha" -- .github/workflows' in upload
    assert 'if [[ -z "$workflow_token" ]]' in upload
    assert "x-access-token:%s" in upload
    # actions/checkout persists its own Authorization extraheader. Git sends every
    # configured value unless the empty entry first resets the inherited header list.
    reset_header = 'git -c "http.https://github.com/.extraheader=" \\'
    workflow_header = '-c "http.https://github.com/.extraheader=AUTHORIZATION: basic $workflow_auth_header" \\'
    assert reset_header in upload
    assert workflow_header in upload
    assert upload.index(reset_header) < upload.index(workflow_header)


def test_propagation_reruns_when_migration_engine_changes() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    push_trigger = workflow.split("  push:", 1)[1].split("  workflow_dispatch:", 1)[0]

    assert '".github/workflows/propagate-native-ai-all-branches.yml"' in push_trigger
    assert '"scripts/migrate_legacy_native_ai.py"' in push_trigger
    assert '"app/prompt_safety.py"' in push_trigger
    assert '"app/groq_ai.py"' in push_trigger



def test_propagation_uses_ancestral_pinned_source_and_semantic_workflow_validation() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    migration = MIGRATION.read_text(encoding="utf-8")

    assert 'git merge-base --is-ancestor "$NATIVE_AI_CANONICAL_COMMIT" "$GITHUB_SHA"' in workflow
    assert workflow.count("':!.github/workflows/production-deploy.yml'") == 1
    assert workflow.count("':!.github/workflows/production-verification.yml'") == 1
    assert "groq_research_review" in workflow
    assert "app/prompt_safety.py" in workflow
    # Workflow labels can mention retired providers without being active wiring. The
    # excluded YAML files are still parsed, scrubbed, and rejected if wiring remains.
    assert "if _workflow_has_active_provider_wiring(cleaned):" in migration
    assert "def scrub_workflow_ancillary(path: str, text: str) -> str:" in migration


def test_update_refs_uses_supported_graphql_fields_and_compare_and_swap() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "refUpdates: [{name: $name, beforeOid: $before, afterOid: $after, force: false}]" in workflow
    assert "mutation($repoId: ID!, $name: GitRefname!, $before: GitObjectID!, $after: GitObjectID!)" in workflow
    assert "mutation($repoId: ID!, $name: String!, $before: GitObjectID!, $after: GitObjectID!)" not in workflow
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
    assert 'NATIVE_AI_CANONICAL_COMMIT="bafcc5bc3e5f025ce59c53555a622c9cb46a2f22"' in workflow
    assert 'NATIVE_AI_CANONICAL_COMMIT="$GITHUB_SHA"' not in workflow
    assert 'NATIVE_AI_CLEANUP_COMMIT="f5e303dad1a93ce1a33940b3e2b16d118897ebf6"' in workflow
    assert 'NATIVE_AI_EXTRA_COMMIT="2317ccc4c3293e8bbd7043970876eceb509d2d1d"' in workflow
    assert 'extra_parent="$(git rev-parse "$NATIVE_AI_EXTRA_COMMIT^")"' in workflow
    assert 'if [[ "$extra_parent" != "$NATIVE_AI_EXTRA_BASE_COMMIT" ]]; then' in workflow
    assert '"app/groq_ai.py"' in runtime_path_list
    assert '"app/prompt_safety.py"' in runtime_path_list
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
    global_scan = next(line for line in workflow.splitlines() if "if matches=\"$(git grep -nIEi" in line)
    assert "groq" not in global_scan.lower()
    assert "':!.github/workflows/production-deploy.yml'" in global_scan
    assert "':!.github/workflows/production-verification.yml'" in global_scan
    assert "scrub_workflow_ancillary(workflow_path.as_posix(), workflow_source)" in workflow
    assert "retired-provider workflow wiring requires migration" in workflow
    assert "if [[ -f metadata.json || -f .github/workflows/production-deploy.yml" in workflow
    assert "legacy_groq_refs" in workflow
    assert r"settings\.groq_(strategy_model|model|api_key)" in workflow
    legacy_groq_scan = next(line for line in workflow.splitlines() if "if legacy_groq_refs=" in line)
    assert "-- app " in legacy_groq_scan
    assert "':!app/groq_ai.py'" in legacy_groq_scan
    assert "':!app/daily_research.py'" not in legacy_groq_scan
    assert "':!app/config.py'" in legacy_groq_scan
    assert "groq_strategy_model" not in legacy_groq_scan
    assert "legacy_groq_config_refs" in workflow
    assert "git grep -nIEi 'groq_strategy_model' -- app/config.py" in workflow
    assert r"api\.cerebras\.ai" in workflow


def test_provider_scan_filters_only_historical_pruning_proof_markers() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "':!.github/workflows/prune-contained-branches-once.yml'" not in workflow
    assert "git grep -nIEi" in workflow
    assert r"prune-contained-branches-once\.yml:[0-9]+" in workflow
    assert "grep -F .def _check_groq." in workflow


def test_metadata_secret_refs_are_checked_even_when_source_grep_has_no_matches() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    helper = MIGRATION.read_text(encoding="utf-8")
    assert 'if (( rc == 1 )); then' in workflow
    assert 'matches=""' in workflow
    assert 'metadata_path.is_file() and helper._metadata_has_active_provider_wiring(metadata_path.read_text(encoding="utf-8"))' in workflow
    assert 'helper_path = Path(os.environ["MIGRATION_HELPER"])' in workflow
    assert "ATLAS_PROVIDER_SECRET_RE" in helper
    assert "metadata.json: active provider configuration or secret reference" in workflow


def test_provider_free_branch_skip_requires_static_native_error_alias_identity() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "def top_level_bound_names(node):" in workflow
    assert "facade_imports_native_error = (" in workflow
    assert "facade_imports[0][2].asname is None" in workflow
    assert "len(facade_error_bindings) == 1" in workflow
    assert "native_alias_bindings" in workflow
    assert "if not native_error_alias or not facade_imports_native_error" in workflow
    assert "app.native_ai.NativeAIError" in workflow or "NativeAI error alias is renamed or rebound" in workflow
    assert "groq_reviewers = [" in workflow
    assert "app.groq_ai reviewer must accept research and intelligence positional arguments" in workflow
    assert "required_positional > 2" in workflow
    assert "required_keyword_only" in workflow
    assert "allowed_plain_imports" in workflow
    assert '(node.level, node.module or "") == (0, "__future__") and alias.asname is not None' in workflow
    assert "if [[ -z \"$before_refs\" ]] && verify_native_ai_impl; then" in workflow
    assert 'importlib.import_module("app.groq_ai")' not in workflow


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


def test_workflow_has_no_duplicated_or_unreachable_tail() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    lines = workflow.splitlines()
    success = '          echo "PASS: all enumerated branches were updated or passed explicit implementation and provider checks."'
    assert lines.count("name: Propagate Atlas Native AI across branches") == 1
    assert lines.count(success) == 1
    assert lines[-1] == success
    assert not any(line.startswith("\\n'") for line in lines)

def test_embedded_native_ai_verifier_script_is_valid_python() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    function_start = workflow.index("verify_native_ai_impl() {")
    heredoc_marker = "python - <<'PY'\n"
    heredoc_start = workflow.index(heredoc_marker, function_start) + len(heredoc_marker)
    heredoc_end = workflow.index("\n          PY\n", heredoc_start)
    embedded_lines = workflow[heredoc_start:heredoc_end].splitlines()
    # Remove the common indentation contributed by the YAML block scalar so this
    # checks the exact Python source the shell heredoc executes.
    source = "\n".join(
        line[10:] if line.startswith("          ") else line
        for line in embedded_lines
    )
    ast.parse(source, filename=f"{WORKFLOW.name}:verify_native_ai_impl")

def test_provider_scan_includes_its_own_workflow_and_filters_only_scanner_literals() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    global_scan = next(
        line for line in workflow.splitlines()
        if 'if matches="$(git grep -nIEi' in line
    )
    assert "':!.github/workflows/propagate-native-ai-all-branches.yml'" not in global_scan
    filter_block = workflow.split("filtered=\"$(printf", 1)[1].split(
        "# Check metadata with the semantic helper", 1
    )[0]
    assert "scanner_prefix =" in filter_block
    assert "current_pattern =" in filter_block
    assert "previous_pattern =" in filter_block
    assert "expected_suffix =" in filter_block
    assert "current_self_excluded_suffix =" in filter_block
    assert "previous_suffix =" in filter_block
    assert "scan_pattern = substr(content, open_quote + 1, close_quote - open_quote - 1)" in filter_block
    assert "if ((scan_pattern == current_pattern && suffix == expected_suffix)" in filter_block
    assert "scan_pattern == previous_pattern && suffix == previous_suffix" in filter_block
    assert "prune-contained-branches-once" in filter_block
    assert r'git show "\$(main_sha|sha):scripts\/check_ai_models\.py"' in filter_block
    assert r'>\/dev\/null[[:space:]]*\\$/' in filter_block
    # Groq research code remains visible to the provider scan.
    assert "legacy_groq_refs" in workflow
    assert "legacy_groq_config_refs" in workflow

def test_provider_scan_filters_only_exact_current_and_previous_scanner_definitions() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    filter_start = workflow.index("filtered=\"$(printf")
    awk_start = workflow.index("awk '\n", filter_start) + len("awk '\n")
    awk_end = workflow.index("\n            ')", awk_start)
    awk_program = workflow[awk_start:awk_end]

    scanner_prefix = (
        ".github/workflows/propagate-native-ai-all-branches.yml:104:"
        "            if matches=\"$(git grep -nIEi '"
    )
    current_pattern = (
        "gemini|cerebras|ai[[:space:]_-]*gateway|generativelanguage\\.googleapis\\.com|"
        "_gemini_json|settings\\.gemini_|settings\\.cerebras_api_key|settings\\.ai_gateway_|"
        "AI_GATEWAY_API_KEY|AI_GATEWAY_BASE_URL|AI_GATEWAY_MODEL|CEREBRAS_API_KEY|CEREBRAS_MODEL|"
        "SECRET_OPENAI|atlas-openai|OPENAI_[[:alnum:]_]+|openai-compatible|api\\.openai\\.com|"
        "SECRET_GEMINI|api\\.cerebras\\.ai"
    )
    previous_pattern = (
        "gemini|groq|cerebras|ai[[:space:]_-]*gateway|generativelanguage\\.googleapis\\.com|"
        "_gemini_json|settings\\.gemini_|settings\\.groq_api_key|settings\\.cerebras_api_key|"
        "settings\\.ai_gateway_|AI_GATEWAY_API_KEY|AI_GATEWAY_BASE_URL|AI_GATEWAY_MODEL|"
        "CEREBRAS_API_KEY|CEREBRAS_MODEL|SECRET_OPENAI|atlas-openai|OPENAI_[[:alnum:]_]+|"
        "openai-compatible|api\\.openai\\.com|SECRET_GEMINI|SECRET_GROQ|GROQ_API_KEY|"
        "api\\.groq\\.com|api\\.cerebras\\.ai"
    )
    common_exclusions = (
        " -- . ':!tests' ':!docs' ':!README.md' ':!CHANGELOG.md' "
    )
    production_exclusions = (
        "':!.github/workflows/production-deploy.yml' "
        "':!.github/workflows/production-verification.yml' "
    )
    migration_exclusion = "':!scripts/migrate_legacy_native_ai.py')\"; then"
    self_exclusion = "':!.github/workflows/propagate-native-ai-all-branches.yml' "

    scanner_definition = (
        scanner_prefix + current_pattern + "'" + common_exclusions
        + production_exclusions + migration_exclusion
    )
    current_self_excluded_scanner_definition = (
        scanner_prefix + current_pattern + "'" + common_exclusions
        + production_exclusions + self_exclusion + migration_exclusion
    )
    previous_scanner_definition = (
        scanner_prefix + previous_pattern + "'" + common_exclusions
        + self_exclusion + migration_exclusion
    )
    modified_scanner_with_extra_command = current_self_excluded_scanner_definition.replace(
        "')\"; then", "')\"; curl https://api.openai.com/v1/models; then"
    )
    modified_previous_scanner_with_extra_command = previous_scanner_definition.replace(
        "')\"; then", "')\"; curl https://api.openai.com/v1/models; then"
    )
    arbitrary_grep_command = (
        ".github/workflows/propagate-native-ai-all-branches.yml:501:"
        "          run: git grep -nIEi 'gemini'; curl -H "
        "\"Authorization: Bearer $OPENAI_API_KEY\" https://api.openai.com/v1/models"
    )
    groq_strategy_match = "app/ai_providers.py:104:async def _groq_json(prompt):"
    groq_config_match = "app/config.py:204:groq_strategy_model: str = 'legacy'"
    historical_prune_marker = (
        ".github/workflows/prune-contained-branches-once.yml:177:"
        "                    && git show \"$main_sha:scripts/check_ai_models.py\" | grep -F 'def _check_groq' >/dev/null \\"
    )
    historical_branch_prune_marker = (
        ".github/workflows/prune-contained-branches-once.yml:178:"
        "                    && ! git show \"$sha:scripts/check_ai_models.py\" | grep -F 'def _check_groq' >/dev/null \\"
    )
    current_pattern_definition = (
        ".github/workflows/propagate-native-ai-all-branches.yml:146:"
        '                    current_pattern = "' + current_pattern.replace("\\", "\\\\") + '"'
    )
    previous_pattern_definition = (
        ".github/workflows/propagate-native-ai-all-branches.yml:147:"
        '                    previous_pattern = "' + previous_pattern.replace("\\", "\\\\") + '"'
    )
    current_pattern_override = (
        ".github/workflows/propagate-native-ai-all-branches.yml:146:"
        '                    current_pattern = "https://api.openai.com/v1/models"'
    )
    previous_pattern_override = (
        ".github/workflows/propagate-native-ai-all-branches.yml:147:"
        '                    previous_pattern = "https://api.groq.com/openai/v1/chat/completions"'
    )
    current_pattern_with_command = current_pattern_definition + "; curl https://api.openai.com/v1/models"
    previous_pattern_with_command = previous_pattern_definition + "; curl https://api.groq.com/openai/v1/chat/completions"
    provider_bearing_prune_command = (
        ".github/workflows/prune-contained-branches-once.yml:177:"
        "                    && git show \"$main_sha:scripts/check_ai_models.py\" | grep -F 'def _check_groq' >/dev/null; curl https://api.openai.com/v1/models"
    )
    source = "\n".join([
        scanner_definition,
        current_self_excluded_scanner_definition,
        previous_scanner_definition,
        modified_scanner_with_extra_command,
        modified_previous_scanner_with_extra_command,
        arbitrary_grep_command,
        groq_strategy_match,
        groq_config_match,
        historical_prune_marker,
        historical_branch_prune_marker,
        current_pattern_definition,
        previous_pattern_definition,
        current_pattern_override,
        previous_pattern_override,
        current_pattern_with_command,
        previous_pattern_with_command,
        provider_bearing_prune_command,
    ])
    result = subprocess.run(
        ["awk", awk_program],
        input=source,
        text=True,
        capture_output=True,
        check=True,
    )
    output_lines = result.stdout.splitlines()

    assert scanner_definition not in output_lines
    assert current_self_excluded_scanner_definition not in output_lines
    assert previous_scanner_definition not in output_lines
    assert modified_scanner_with_extra_command in output_lines
    assert modified_previous_scanner_with_extra_command in output_lines
    assert arbitrary_grep_command in output_lines
    assert groq_strategy_match in output_lines
    assert groq_config_match in output_lines
    assert historical_prune_marker not in output_lines
    assert historical_branch_prune_marker not in output_lines
    assert current_pattern_definition not in output_lines
    assert previous_pattern_definition not in output_lines
    assert current_pattern_override in output_lines
    assert previous_pattern_override in output_lines
    assert current_pattern_with_command in output_lines
    assert previous_pattern_with_command in output_lines
    assert provider_bearing_prune_command in output_lines

def test_post_migration_diagnostics_reuse_scan_results_without_second_grep() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    after_refs_block = workflow.split('if [[ -n "$after_refs" ]]; then', 1)[1].split(
        "if ! verify_native_ai_impl(); then", 1
    )[0]
    assert 'printf \'%s\\n\' "$after_refs" >&2' in after_refs_block
    assert "git grep -nIEi" not in after_refs_block
