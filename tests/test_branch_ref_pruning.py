from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import scripts.prune_safe_merged_branch_refs as pruning
from scripts.prune_safe_merged_branch_refs import should_delete_ref


def _ancestor_pairs(*pairs: tuple[str, str]):
    known = set(pairs)
    return lambda ancestor, descendant: (ancestor, descendant) in known


def test_branch_pruning_never_deletes_main_or_a_branch_with_an_open_pr():
    is_ancestor = _ancestor_pairs(("tip", "main"))

    assert should_delete_ref("main", "tip", "main", set(), [], is_ancestor) == (
        False,
        "protected main ref",
    )
    assert should_delete_ref(
        "feature", "tip", "main", {"feature"}, [], is_ancestor
    ) == (False, "branch has an open pull request")


def test_branch_pruning_accepts_only_a_branch_tip_that_is_in_main_history():
    decision = should_delete_ref(
        "old-merged-branch",
        "branch-tip",
        "main-tip",
        set(),
        [],
        _ancestor_pairs(("branch-tip", "main-tip")),
    )

    assert decision == (True, "exact branch tip is an ancestor of main")


def test_branch_pruning_accepts_squash_merge_only_for_exact_recorded_head():
    merged_prs = [
        {
            "headRefName": "paper-fix",
            "headRefOid": "exact-branch-head",
            "mergeCommit": {"oid": "squash-merge-sha"},
        }
    ]
    decision = should_delete_ref(
        "paper-fix",
        "exact-branch-head",
        "main-tip",
        set(),
        merged_prs,
        _ancestor_pairs(("squash-merge-sha", "main-tip")),
    )

    assert decision == (
        True,
        "exact branch tip was merged via commit squash-merge-sha on main",
    )


def test_branch_pruning_preserves_a_branch_advanced_after_its_pr_was_merged():
    merged_prs = [
        {
            "headRefName": "paper-fix",
            "headRefOid": "older-merged-head",
            "mergeCommit": {"oid": "squash-merge-sha"},
        }
    ]
    decision = should_delete_ref(
        "paper-fix",
        "newer-unmerged-head",
        "main-tip",
        set(),
        merged_prs,
        _ancestor_pairs(("squash-merge-sha", "main-tip")),
    )

    assert decision == (False, "no exact integration proof; preserve branch")


def test_branch_pruning_preserves_a_merged_pr_whose_merge_commit_is_not_on_main():
    merged_prs = [
        {
            "headRefName": "paper-fix",
            "headRefOid": "exact-branch-head",
            "mergeCommit": {"oid": "merge-not-in-main"},
        }
    ]
    decision = should_delete_ref(
        "paper-fix",
        "exact-branch-head",
        "main-tip",
        set(),
        merged_prs,
        _ancestor_pairs(),
    )

    assert decision == (False, "no exact integration proof; preserve branch")


def test_branch_pruning_preserves_unmerged_branch_with_no_integration_evidence():
    decision = should_delete_ref(
        "unique-research-branch",
        "unique-tip",
        "main-tip",
        set(),
        [],
        _ancestor_pairs(),
    )

    assert decision == (False, "no exact integration proof; preserve branch")


def test_branch_delta_matches_main_when_every_branch_changed_file_matches(
    monkeypatch,
):
    def fake_run(args, *, check=True):
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 0, "base-sha\n", "")
        if args[:5] == ["git", "diff", "--no-renames", "--name-only", "-z"]:
            return subprocess.CompletedProcess(
                args, 0, "app/module.py\0docs/readme.md\0", ""
            )
        if args[:4] == ["git", "--literal-pathspecs", "ls-tree", "-z"]:
            expression = f"{args[6]}:{args[8]}"
            entries = {
                "branch-sha:app/module.py": ("100644", "blob", "blob-one"),
                "main-sha:app/module.py": ("100644", "blob", "blob-one"),
                "branch-sha:docs/readme.md": ("100644", "blob", "blob-two"),
                "main-sha:docs/readme.md": ("100644", "blob", "blob-two"),
            }
            entry = entries.get(expression)
            output = f"{entry[0]} {entry[1]} {entry[2]}\t{args[8]}\0" if entry else ""
            return subprocess.CompletedProcess(args, 0, output, "")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(pruning, "_run", fake_run)

    assert pruning._branch_delta_matches_main("branch-sha", "main-sha") is True


def test_branch_delta_preserves_any_changed_file_not_matching_main(monkeypatch):
    def fake_run(args, *, check=True):
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 0, "base-sha\n", "")
        if args[:5] == ["git", "diff", "--no-renames", "--name-only", "-z"]:
            return subprocess.CompletedProcess(args, 0, "app/module.py\0", "")
        if args[:4] == ["git", "--literal-pathspecs", "ls-tree", "-z"]:
            expression = f"{args[6]}:{args[8]}"
            entries = {
                "branch-sha:app/module.py": ("100644", "blob", "branch-blob"),
                "main-sha:app/module.py": ("100644", "blob", "main-blob"),
            }
            entry = entries.get(expression)
            output = f"{entry[0]} {entry[1]} {entry[2]}\t{args[8]}\0" if entry else ""
            return subprocess.CompletedProcess(args, 0, output, "")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(pruning, "_run", fake_run)

    assert pruning._branch_delta_matches_main("branch-sha", "main-sha") is False


def test_branch_delta_accepts_same_deleted_path_on_both_sides(monkeypatch):
    def fake_run(args, *, check=True):
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 0, "base-sha\n", "")
        if args[:5] == ["git", "diff", "--no-renames", "--name-only", "-z"]:
            return subprocess.CompletedProcess(args, 0, "old-module.py\0", "")
        if args[:4] == ["git", "--literal-pathspecs", "ls-tree", "-z"]:
            # A deleted path has no entry in either commit tree.
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(pruning, "_run", fake_run)

    assert pruning._branch_delta_matches_main("branch-sha", "main-sha") is True


def test_branch_delta_does_not_ignore_unreadable_git_state(monkeypatch):
    def fake_run(args, *, check=True):
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 1, "", "fatal: repository damaged")
        raise AssertionError("diff must not run when merge-base cannot be read")

    monkeypatch.setattr(pruning, "_run", fake_run)

    with pytest.raises(RuntimeError, match="merge-base failed"):
        pruning._branch_delta_matches_main("branch-sha", "main-sha")


def test_missing_merged_pr_commit_is_unproven_not_fatal(monkeypatch):
    def fake_run(args, *, check=True):
        return subprocess.CompletedProcess(
            args, 128, "", "fatal: Not a valid commit name deadbeef"
        )

    monkeypatch.setattr(pruning, "_run", fake_run)

    assert pruning._is_ancestor("deadbeef", "main-sha") is False


def test_missing_commit_marker_mixed_with_object_database_error_aborts(monkeypatch):
    def fake_run(args, *, check=True):
        return subprocess.CompletedProcess(
            args,
            128,
            "",
            "fatal: Not a valid commit name deadbeef\nfatal: loose object deadbeef is corrupt",
        )

    monkeypatch.setattr(pruning, "_run", fake_run)

    with pytest.raises(RuntimeError, match="merge-base failed"):
        pruning._is_ancestor("deadbeef", "main-sha")


def test_branch_protection_status_is_parsed_and_protected_branches_are_identified(monkeypatch):
    def fake_run(args, *, check=True):
        assert args[:2] == ["gh", "api"]
        return subprocess.CompletedProcess(args, 0, "main\tfalse\nfeature/keep\ttrue\n", "")

    monkeypatch.setattr(pruning, "_run", fake_run)

    assert pruning._load_branch_protection_status("Papajona/Atlas-Bot") == {
        "main": False,
        "feature/keep": True,
    }


def test_branch_protection_status_fails_closed_on_api_error(monkeypatch):
    def fake_run(args, *, check=True):
        assert args[:2] == ["gh", "api"]
        return subprocess.CompletedProcess(args, 1, "", "permission denied")

    monkeypatch.setattr(pruning, "_run", fake_run)

    with pytest.raises(RuntimeError, match="protection status"):
        pruning._load_branch_protection_status("Papajona/Atlas-Bot")


def test_branch_protection_status_rejects_malformed_data(monkeypatch):
    def fake_run(args, *, check=True):
        return subprocess.CompletedProcess(args, 0, "main\tfalse\nfeature\tunknown\n", "")

    monkeypatch.setattr(pruning, "_run", fake_run)

    with pytest.raises(RuntimeError, match="malformed"):
        pruning._load_branch_protection_status("Papajona/Atlas-Bot")


def test_branch_parity_mismatches_are_computed_once_for_preserved_branch(monkeypatch):
    calls = []

    monkeypatch.setattr(pruning, "_is_ancestor", lambda ancestor, descendant: False)

    def mismatches(branch_sha, main_sha):
        calls.append((branch_sha, main_sha))
        return ["scripts/check.sh"]

    monkeypatch.setattr(pruning, "_branch_delta_mismatched_paths", mismatches)

    safe, reason, mismatch_paths = pruning._resolve_branch_integration(
        "mode-only",
        "branch-sha",
        "main-sha",
        set(),
        [],
    )

    assert safe is False
    assert reason == "no exact integration proof; preserve branch"
    assert mismatch_paths == ["scripts/check.sh"]
    assert calls == [("branch-sha", "main-sha")]


def test_deletion_of_already_absent_remote_ref_is_a_successful_noop(monkeypatch):
    def fake_run(args, *, check=True):
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, 1, "", "remote rejected deletion")
        if args[:3] == ["git", "ls-remote", "--heads"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(pruning, "_run", fake_run)

    status, reason = pruning._delete_remote_ref_with_lease("feature/already-gone", "abc123")

    assert status == "PRESERVE"
    assert "already removed" in reason
    assert "successful no-op" in reason




def test_deletion_lease_rejection_preserves_remote_branch_that_advanced(monkeypatch):
    def fake_run(args, *, check=True):
        if args[:2] == ["git", "push"]:
            assert args[2] == "origin"
            assert args[3] == (
                "--force-with-lease=refs/heads/feature/advancing:proven-sha"
            )
            assert args[4] == ":refs/heads/feature/advancing"
            return subprocess.CompletedProcess(
                args, 1, "", "! [rejected] feature/advancing (stale info)"
            )
        if args[:3] == ["git", "ls-remote", "--heads"]:
            return subprocess.CompletedProcess(
                args, 0,
                "advanced-sha\trefs/heads/feature/advancing\n",
                "",
            )
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(pruning, "_run", fake_run)

    status, reason = pruning._delete_remote_ref_with_lease(
        "feature/advancing", "proven-sha"
    )

    assert status == "PRESERVE"
    assert "lease was rejected" in reason
    assert "branch preserved" in reason


def test_unknown_protection_branch_is_removed_only_when_remote_absence_is_verified(monkeypatch):
    def fake_run(args, *, check=True):
        assert args == ["git", "ls-remote", "--heads", "origin", "refs/heads/stale/branch"]
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(pruning, "_run", fake_run)

    assert pruning._reconcile_unknown_protection_branches(
        ["active/branch", "stale/branch"],
        {"active/branch": False},
    ) == ["active/branch"]


def test_unknown_protection_branch_still_remote_aborts_pruning(monkeypatch):
    def fake_run(args, *, check=True):
        return subprocess.CompletedProcess(args, 0, "abc123\\trefs/heads/stale/branch\\n", "")

    monkeypatch.setattr(pruning, "_run", fake_run)

    with pytest.raises(RuntimeError, match="still exists remotely"):
        pruning._reconcile_unknown_protection_branches(
            ["stale/branch"], {}
        )


def test_unknown_protection_branch_unreadable_remote_status_aborts_pruning(monkeypatch):
    def fake_run(args, *, check=True):
        return subprocess.CompletedProcess(args, 128, "", "network unavailable")

    monkeypatch.setattr(pruning, "_run", fake_run)

    with pytest.raises(RuntimeError, match="Unable to read remote status"):
        pruning._reconcile_unknown_protection_branches(["stale/branch"], {})


def test_branch_tree_lookup_ignores_descendants_when_path_is_a_directory_on_main(monkeypatch):
    def fake_run(args, *, check=True):
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 0, "base-sha\n", "")
        if args[:5] == ["git", "diff", "--no-renames", "--name-only", "-z"]:
            return subprocess.CompletedProcess(args, 0, "app/module.py\0", "")
        if args[:4] == ["git", "--literal-pathspecs", "ls-tree", "-z"]:
            commit_sha = args[6]
            path = args[8]
            if commit_sha == "branch-sha":
                return subprocess.CompletedProcess(
                    args, 0, f"100644 blob branch-blob\t{path}\0", ""
                )
            if commit_sha == "main-sha":
                return subprocess.CompletedProcess(
                    args, 0,
                    f"040000 tree main-tree\t{path}\0"
                    f"100644 blob descendant\t{path}/child.txt\0",
                    "",
                )
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(pruning, "_run", fake_run)

    assert pruning._branch_delta_matches_main("branch-sha", "main-sha") is False


def test_unexpected_merge_base_error_still_aborts(monkeypatch):
    def fake_run(args, *, check=True):
        return subprocess.CompletedProcess(
            args, 128, "", "fatal: repository object database corrupted"
        )

    monkeypatch.setattr(pruning, "_run", fake_run)

    with pytest.raises(RuntimeError, match="merge-base failed"):
        pruning._is_ancestor("merge-sha", "main-sha")



def _safe_android_release_workflow() -> str:
    return """name: Android release
on:
  workflow_dispatch:
  push:
    tags:
      - "android-v*"
permissions:
  contents: read
jobs:
  android-release:
    if: (github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main') || (github.event_name == 'push' && startsWith(github.ref, 'refs/tags/android-v'))
    runs-on: ubuntu-latest
"""


def _safe_android_release_test_source() -> str:
    return """def test_signed_release_is_only_triggered_by_an_explicit_release_tag_or_manual_main_dispatch():
    assert "branches:" not in push_trigger
    assert "paths:" not in push_trigger
    assert "github.event_name == 'push' && github.ref == 'refs/heads/main'" not in release_job
"""


def test_android_gate_allows_harmless_release_job_name_metadata(monkeypatch):
    branch_name = "fix/android-release-trigger-gating-followup-20261023"
    branch_sha = "6b9feef6b55be4ef06eef630d4b558e0ffad35b7"
    named_workflow = _safe_android_release_workflow().replace(
        "  android-release:\\n",
        "  android-release:\\n    name: Signed Android release\\n",
    )
    monkeypatch.setattr(pruning, "_is_ancestor", lambda *_args: False)
    monkeypatch.setattr(
        pruning,
        "_branch_delta_mismatched_paths",
        lambda *_args: ["tests/test_android_production_backend_wiring.py"],
    )
    monkeypatch.setattr(
        pruning,
        "_git_file_text_at_commit",
        lambda _commit, path: (
            named_workflow
            if path == ".github/workflows/android-release.yml"
            else _safe_android_release_test_source()
        ),
    )

    safe, reason, mismatches = pruning._resolve_branch_integration(
        branch_name, branch_sha, "main-sha", set(), []
    )

    assert safe is True
    assert "exact historical Android release-trigger branch superseded" in reason
    assert mismatches == []


def test_known_android_gate_branch_is_pruned_only_after_main_has_stronger_gate(monkeypatch):
    branch_name = "fix/android-release-trigger-gating-followup-20261023"
    branch_sha = "6b9feef6b55be4ef06eef630d4b558e0ffad35b7"
    monkeypatch.setattr(pruning, "_is_ancestor", lambda *_args: False)
    monkeypatch.setattr(
        pruning,
        "_branch_delta_mismatched_paths",
        lambda *_args: ["tests/test_android_production_backend_wiring.py"],
    )
    monkeypatch.setattr(
        pruning,
        "_git_file_text_at_commit",
        lambda _commit, path: (
            _safe_android_release_workflow()
            if path == ".github/workflows/android-release.yml"
            else _safe_android_release_test_source()
        ),
    )

    safe, reason, mismatches = pruning._resolve_branch_integration(
        branch_name, branch_sha, "main-sha", set(), []
    )

    assert safe is True
    assert "exact historical Android release-trigger branch superseded" in reason
    assert mismatches == []


def test_android_gate_supersession_does_not_match_an_unknown_or_advanced_sha(monkeypatch):
    branch_name = "fix/android-release-trigger-gating-followup-20261023"
    monkeypatch.setattr(pruning, "_is_ancestor", lambda *_args: False)
    monkeypatch.setattr(
        pruning,
        "_branch_delta_mismatched_paths",
        lambda *_args: ["tests/test_android_production_backend_wiring.py"],
    )

    safe, reason, mismatches = pruning._resolve_branch_integration(
        branch_name, "new-unreviewed-sha", "main-sha", set(), []
    )

    assert safe is False
    assert reason == "no exact integration proof; preserve branch"
    assert mismatches == ["tests/test_android_production_backend_wiring.py"]


def test_android_gate_supersession_preserves_branch_if_main_workflow_is_not_safe(monkeypatch):
    branch_name = "fix/android-release-trigger-gating-followup-20261023"
    branch_sha = "6b9feef6b55be4ef06eef630d4b558e0ffad35b7"
    unsafe_workflow = _safe_android_release_workflow().replace(
        'tags:\n      - "android-v*"',
        'branches:\n      - main\n    tags:\n      - "android-v*"',
    ).replace(
        " || (github.event_name == 'push' && startsWith(github.ref, 'refs/tags/android-v'))",
        " || (github.event_name == 'push' && github.ref == 'refs/heads/main') || "
        "(github.event_name == 'push' && startsWith(github.ref, 'refs/tags/android-v'))",
    )
    monkeypatch.setattr(pruning, "_is_ancestor", lambda *_args: False)
    monkeypatch.setattr(
        pruning,
        "_branch_delta_mismatched_paths",
        lambda *_args: ["tests/test_android_production_backend_wiring.py"],
    )
    monkeypatch.setattr(
        pruning,
        "_git_file_text_at_commit",
        lambda _commit, path: (
            unsafe_workflow
            if path == ".github/workflows/android-release.yml"
            else _safe_android_release_test_source()
        ),
    )

    safe, reason, mismatches = pruning._resolve_branch_integration(
        branch_name, branch_sha, "main-sha", set(), []
    )

    assert safe is False
    assert reason == "no exact integration proof; preserve branch"
    assert mismatches == ["tests/test_android_production_backend_wiring.py"]


def test_android_gate_supersession_rejects_extra_release_job_alternative(monkeypatch):
    branch_name = "fix/android-release-trigger-gating-followup-20261023"
    branch_sha = "6b9feef6b55be4ef06eef630d4b558e0ffad35b7"
    safe_workflow = _safe_android_release_workflow()
    unsafe_workflow = safe_workflow.replace(
        "startsWith(github.ref, 'refs/tags/android-v'))",
        "startsWith(github.ref, 'refs/tags/android-v')) || "
        "(github.event_name == 'pull_request')",
    )
    assert unsafe_workflow != safe_workflow
    monkeypatch.setattr(pruning, "_is_ancestor", lambda *_args: False)
    monkeypatch.setattr(
        pruning,
        "_branch_delta_mismatched_paths",
        lambda *_args: ["tests/test_android_production_backend_wiring.py"],
    )
    monkeypatch.setattr(
        pruning,
        "_git_file_text_at_commit",
        lambda _commit, path: (
            unsafe_workflow
            if path == ".github/workflows/android-release.yml"
            else _safe_android_release_test_source()
        ),
    )

    safe, reason, mismatches = pruning._resolve_branch_integration(
        branch_name, branch_sha, "main-sha", set(), []
    )

    assert safe is False
    assert reason == "no exact integration proof; preserve branch"
    assert mismatches == ["tests/test_android_production_backend_wiring.py"]


def test_android_gate_supersession_rejects_pull_request_continuation_line(monkeypatch):
    branch_name = "fix/android-release-trigger-gating-followup-20261023"
    branch_sha = "6b9feef6b55be4ef06eef630d4b558e0ffad35b7"
    safe_workflow = _safe_android_release_workflow()
    expected = (
        "(github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main') || "
        "(github.event_name == 'push' && startsWith(github.ref, 'refs/tags/android-v'))"
    )
    unsafe_workflow = safe_workflow.replace(
        f"    if: {expected}",
        f"    if: {expected}\n      || (github.event_name == 'pull_request')",
    )
    assert unsafe_workflow != safe_workflow
    monkeypatch.setattr(pruning, "_is_ancestor", lambda *_args: False)
    monkeypatch.setattr(
        pruning, "_branch_delta_mismatched_paths",
        lambda *_args: ["tests/test_android_production_backend_wiring.py"],
    )
    monkeypatch.setattr(
        pruning, "_git_file_text_at_commit",
        lambda _commit, path: (
            unsafe_workflow
            if path == ".github/workflows/android-release.yml"
            else _safe_android_release_test_source()
        ),
    )

    safe, reason, mismatches = pruning._resolve_branch_integration(
        branch_name, branch_sha, "main-sha", set(), []
    )

    assert safe is False
    assert reason == "no exact integration proof; preserve branch"
    assert mismatches == ["tests/test_android_production_backend_wiring.py"]


def test_android_gate_supersession_requires_executable_regression_test(monkeypatch):
    branch_name = "fix/android-release-trigger-gating-followup-20261023"
    branch_sha = "6b9feef6b55be4ef06eef630d4b558e0ffad35b7"
    monkeypatch.setattr(pruning, "_is_ancestor", lambda *_args: False)
    monkeypatch.setattr(
        pruning, "_branch_delta_mismatched_paths",
        lambda *_args: ["tests/test_android_production_backend_wiring.py"],
    )
    monkeypatch.setattr(
        pruning, "_git_file_text_at_commit",
        lambda _commit, path: (
            _safe_android_release_workflow()
            if path == ".github/workflows/android-release.yml"
            else "# test_signed_release_is_only_triggered_by_an_explicit_release_tag_or_manual_main_dispatch"
        ),
    )

    safe, reason, mismatches = pruning._resolve_branch_integration(
        branch_name, branch_sha, "main-sha", set(), []
    )

    assert safe is False
    assert reason == "no exact integration proof; preserve branch"
    assert mismatches == ["tests/test_android_production_backend_wiring.py"]


def test_android_gate_supersession_preserves_unrelated_branch_path_changes(monkeypatch):
    branch_name = "fix/android-release-trigger-gating-20261023"
    branch_sha = "fc84eac73078c65a8c253101c40e963958b74e60"
    monkeypatch.setattr(pruning, "_is_ancestor", lambda *_args: False)
    monkeypatch.setattr(
        pruning,
        "_branch_delta_mismatched_paths",
        lambda *_args: ["tests/test_android_production_backend_wiring.py", "app/main.py"],
    )
    monkeypatch.setattr(
        pruning,
        "_git_file_text_at_commit",
        lambda _commit, path: (
            _safe_android_release_workflow()
            if path == ".github/workflows/android-release.yml"
            else _safe_android_release_test_source()
        ),
    )

    safe, reason, mismatches = pruning._resolve_branch_integration(
        branch_name, branch_sha, "main-sha", set(), []
    )

    assert safe is False
    assert reason == "no exact integration proof; preserve branch"
    assert mismatches == [
        "tests/test_android_production_backend_wiring.py",
        "app/main.py",
    ]


def test_android_gate_branch_with_open_pr_is_preserved_even_when_superseded(monkeypatch):
    branch_name = "fix/android-release-trigger-gating-followup-20261023"
    branch_sha = "6b9feef6b55be4ef06eef630d4b558e0ffad35b7"
    monkeypatch.setattr(pruning, "_is_ancestor", lambda *_args: False)

    safe, reason, mismatches = pruning._resolve_branch_integration(
        branch_name, branch_sha, "main-sha", {branch_name}, []
    )

    assert safe is False
    assert reason == "branch has an open pull request"
    assert mismatches == []


def _git(cwd, *args: str) -> str:
    result = subprocess.run(
        ["git", *map(str, args)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"git {' '.join(map(str, args))} failed: "
        f"{result.stderr.strip() or result.stdout.strip()}"
    )
    return result.stdout.strip()


def test_mode_only_executable_bit_change_preserves_branch(tmp_path, monkeypatch):
    repo = tmp_path / "mode-repo"
    _git(tmp_path, "init", "-b", "main", repo)
    _git(repo, "config", "user.name", "Atlas mode regression")
    _git(repo, "config", "user.email", "atlas-mode@example.invalid")
    check_script = repo / "scripts" / "check.sh"
    check_script.parent.mkdir()
    check_script.write_text("#!/bin/sh\necho safe\n", encoding="utf-8")
    check_script.chmod(0o644)
    _git(repo, "add", "scripts/check.sh")
    _git(repo, "commit", "-m", "base non-executable script")
    main_sha = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-b", "mode-only")
    check_script.chmod(0o755)
    _git(repo, "add", "scripts/check.sh")
    _git(repo, "commit", "-m", "change executable bit only")
    branch_sha = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)

    mismatch_paths = pruning._branch_delta_mismatched_paths(branch_sha, main_sha)
    assert mismatch_paths == ["scripts/check.sh"]
    safe, reason, observed_mismatches = pruning._resolve_branch_integration(
        "mode-only",
        branch_sha,
        main_sha,
        set(),
        [],
    )
    assert safe is False
    assert observed_mismatches == ["scripts/check.sh"]
    assert "preserve branch" in reason


def test_lease_rejection_preserves_branch_updated_after_proof(tmp_path, monkeypatch):
    """A newer remote tip must survive if it advances after the proof SHA was read."""
    remote = tmp_path / "origin.git"
    checkout = tmp_path / "checkout"
    advancer = tmp_path / "advancer"
    branch_name = "feature/lease-race"

    _git(tmp_path, "init", "--bare", remote)
    _git(tmp_path, "init", "-b", "main", checkout)
    _git(checkout, "config", "user.name", "Atlas regression test")
    _git(checkout, "config", "user.email", "atlas-test@example.invalid")
    (checkout / "README.md").write_text("base\n", encoding="utf-8")
    _git(checkout, "add", "README.md")
    _git(checkout, "commit", "-m", "test base")
    _git(checkout, "remote", "add", "origin", remote)
    _git(checkout, "push", "-u", "origin", "main")

    _git(checkout, "checkout", "-b", branch_name)
    (checkout / "feature.txt").write_text("integrated version\n", encoding="utf-8")
    _git(checkout, "add", "feature.txt")
    _git(checkout, "commit", "-m", "branch version used for proof")
    _git(checkout, "push", "-u", "origin", branch_name)
    proven_sha = _git(checkout, "rev-parse", "HEAD")

    # An independent writer advances the remote after the cleanup proof was made.
    _git(tmp_path, "clone", "--branch", branch_name, remote, advancer)
    _git(advancer, "config", "user.name", "Atlas branch advancer")
    _git(advancer, "config", "user.email", "atlas-advancer@example.invalid")
    with (advancer / "feature.txt").open("a", encoding="utf-8") as feature_file:
        feature_file.write("unreviewed update that must survive\n")
    _git(advancer, "commit", "-am", "advance branch after proof")
    _git(advancer, "push", "origin", branch_name)
    advanced_sha = _git(advancer, "rev-parse", "HEAD")
    assert advanced_sha != proven_sha

    # Run the actual lease-protected push from the stale cleanup checkout.
    monkeypatch.chdir(checkout)
    status, reason = pruning._delete_remote_ref_with_lease(branch_name, proven_sha)

    assert status == "PRESERVE"
    assert "lease was rejected" in reason
    remote_listing = _git(advancer, "ls-remote", "origin", f"refs/heads/{branch_name}")
    assert remote_listing.split()[0] == advanced_sha


def test_native_ai_branch_retirement_requires_exact_sha_and_migration_only_paths(monkeypatch):
    branch_name = "fix/native-ai-propagation-error-alias-20261027"
    expected_sha = "a7f35abfe8d3a73af329c5ae34320afe5f0def12"
    migration_paths = sorted(pruning._NATIVE_AI_MIGRATION_ARTIFACT_PATHS)
    monkeypatch.setattr(
        pruning, "_main_has_verified_native_ai_migration", lambda _sha: True
    )

    assert pruning._is_explicitly_superseded_native_ai_migration_ref(
        branch_name, expected_sha, "verified-main-sha", migration_paths
    )
    assert pruning._is_explicitly_superseded_native_ai_migration_ref(
        "fix/remediation-dc7d0b60-cf91de",
        "fc2776a36358cf22e4e677737ad0230464cb414a",
        "verified-main-sha",
        [
            "scripts/migrate_legacy_native_ai.py",
            "tests/test_legacy_native_ai_migration.py",
        ],
    )
    monkeypatch.setattr(
        pruning,
        "_is_ancestor",
        lambda ancestor, descendant: (
            ancestor == expected_sha and descendant == "verified-descendant-sha"
        ),
    )
    assert not pruning._is_explicitly_superseded_native_ai_migration_ref(
        branch_name, "verified-descendant-sha", "verified-main-sha", migration_paths
    )
    assert not pruning._is_explicitly_superseded_native_ai_migration_ref(
        branch_name, "changed-branch-sha", "verified-main-sha", migration_paths
    )
    assert not pruning._is_explicitly_superseded_native_ai_migration_ref(
        branch_name, expected_sha, "verified-main-sha", migration_paths + ["app/main.py"]
    )
    assert not pruning._is_explicitly_superseded_native_ai_migration_ref(
        "unlisted-branch", expected_sha, "verified-main-sha", migration_paths
    )


def test_native_ai_retirement_preserves_descendant_with_unmerged_migration_edit(tmp_path, monkeypatch):
    local_repo = tmp_path / "native-ai-descendant-repo"
    _git(tmp_path, "init", "-b", "main", local_repo)
    _git(local_repo, "config", "user.name", "Atlas branch pruning regression")
    _git(local_repo, "config", "user.email", "atlas-prune@example.invalid")

    migration_path = local_repo / "scripts" / "migrate_legacy_native_ai.py"
    migration_path.parent.mkdir(parents=True)
    migration_path.write_text("AUDITED_MIGRATION = True\n", encoding="utf-8")
    _git(local_repo, "add", "scripts/migrate_legacy_native_ai.py")
    _git(local_repo, "commit", "-m", "audited migration tip")
    audited_sha = _git(local_repo, "rev-parse", "HEAD")

    migration_path.write_text(
        "AUDITED_MIGRATION = True\nUNREVIEWED_EDIT = True\n",
        encoding="utf-8",
    )
    _git(local_repo, "add", "scripts/migrate_legacy_native_ai.py")
    _git(local_repo, "commit", "-m", "unmerged migration edit")
    descendant_sha = _git(local_repo, "rev-parse", "HEAD")
    assert _git(local_repo, "merge-base", "--is-ancestor", audited_sha, descendant_sha) == ""

    branch_name = "fix/native-ai-propagation-error-alias-20261027"
    monkeypatch.setattr(
        pruning,
        "_SUPERSEDED_NATIVE_AI_MIGRATION_BRANCHES",
        {branch_name: audited_sha},
    )

    def local_is_ancestor(ancestor, descendant):
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=local_repo,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    monkeypatch.setattr(pruning, "_is_ancestor", local_is_ancestor)
    monkeypatch.setattr(
        pruning, "_branch_delta_mismatched_paths",
        lambda *_args: ["scripts/migrate_legacy_native_ai.py"],
    )
    monkeypatch.setattr(
        pruning, "_main_has_verified_native_ai_migration", lambda _sha: True
    )

    safe, reason, mismatch_paths = pruning._resolve_branch_integration(
        branch_name, descendant_sha, audited_sha, set(), []
    )
    assert safe is False
    assert reason == "no exact integration proof; preserve branch"
    assert mismatch_paths == ["scripts/migrate_legacy_native_ai.py"]


def test_native_ai_branch_retirement_gate_accepts_committed_migration_safeguards():
    """Read the real committed tree; the success path must not mock git show."""
    repository_root = Path(__file__).resolve().parents[1]
    verified_main_sha = _git(repository_root, "rev-parse", "HEAD")

    assert pruning._main_has_verified_native_ai_migration(verified_main_sha)
    assert pruning._is_explicitly_superseded_native_ai_migration_ref(
        "fix/native-ai-propagation-error-alias-20261027",
        "a7f35abfe8d3a73af329c5ae34320afe5f0def12",
        verified_main_sha,
        sorted(pruning._NATIVE_AI_MIGRATION_ARTIFACT_PATHS),
    )


def test_native_ai_branch_retirement_fails_closed_when_a_required_guard_is_missing(monkeypatch):
    sources = {
        "scripts/migrate_legacy_native_ai.py": "\n".join((
            "def _is_replaceable_legacy_market_context(): pass",
            "def _safe_canonical_trade_safety_validator(): pass",
        )),
        "tests/test_legacy_native_ai_migration.py": "\n".join((
            "import unittest",
            "class MigrationTests(unittest.TestCase):",
            "    def test_strategy_migration_accepts_recognized_two_and_three_provider_flows(self): pass",
            "    def test_legacy_market_context_rejects_rebinding_and_dynamic_lookups(self): pass",
            "    def test_equivalent_legacy_trade_safety_validator_is_deduplicated(self): pass",
            # Intentionally omit the end-to-end safety-validator regression.
        )),
    }
    monkeypatch.setattr(
        pruning, "_git_file_text_at_commit", lambda _sha, path: sources[path]
    )
    assert not pruning._main_has_verified_native_ai_migration("missing-required-test-sha")


def test_native_ai_retirement_does_not_accept_signatures_inside_multiline_strings(monkeypatch):
    sources = {
        "scripts/migrate_legacy_native_ai.py": (
            '"""\n'
            "def _is_compatible_strategy_schema_and_context(): pass\n"
            "def _safe_canonical_json_helper(): pass\n"
            '"""\n'
        ),
        "tests/test_legacy_native_ai_migration.py": (
            '"""\n'
            "def test_strategy_migration_accepts_recognized_two_and_three_provider_flows(self): pass\n"
            "def test_exact_schema_contract_rejects_missing_canonical_validator(self): pass\n"
            "def test_exact_schema_contract_rejects_market_binding_before_model_dump(self): pass\n"
            "def test_legacy_extract_json_uses_canonical_pure_parser_contract(self): pass\n"
            "def test_legacy_response_json_helper_uses_canonical_parser_contract(self): pass\n"
            '"""\n'
        ),
    }
    monkeypatch.setattr(
        pruning, "_git_file_text_at_commit", lambda _sha, path: sources[path]
    )
    assert not pruning._main_has_verified_native_ai_migration("quoted-markers-only-sha")


def test_native_ai_retirement_rejects_async_guard_definitions(monkeypatch):
    sources = {
        "scripts/migrate_legacy_native_ai.py": "\n".join((
            "async def _is_replaceable_legacy_market_context(): pass",
            "async def _safe_canonical_trade_safety_validator(): pass",
        )),
        "tests/test_legacy_native_ai_migration.py": "\n".join((
            "import unittest",
            "class MigrationTests(unittest.TestCase):",
            "    async def test_strategy_migration_accepts_recognized_two_and_three_provider_flows(self): pass",
            "    async def test_legacy_market_context_rejects_rebinding_and_dynamic_lookups(self): pass",
            "    async def test_equivalent_legacy_trade_safety_validator_is_deduplicated(self): pass",
            "    async def test_ai_provider_migration_accepts_only_equivalent_trade_safety_validator(self): pass",
        )),
    }
    monkeypatch.setattr(
        pruning, "_git_file_text_at_commit", lambda _sha, path: sources[path]
    )
    assert not pruning._main_has_verified_native_ai_migration("async-guards-only-sha")


def test_native_ai_retirement_keeps_branches_with_open_prs(monkeypatch):
    branch_name = "fix/native-ai-propagation-error-alias-20261027"
    branch_sha = "a7f35abfe8d3a73af329c5ae34320afe5f0def12"
    monkeypatch.setattr(pruning, "_is_ancestor", lambda *_args: False)
    monkeypatch.setattr(
        pruning, "_branch_delta_mismatched_paths",
        lambda *_args: sorted(pruning._NATIVE_AI_MIGRATION_ARTIFACT_PATHS),
    )
    monkeypatch.setattr(
        pruning, "_main_has_verified_native_ai_migration", lambda _sha: True
    )

    safe, reason, paths = pruning._resolve_branch_integration(
        branch_name, branch_sha, "verified-main-sha", {branch_name}, []
    )
    assert safe is False
    assert reason == "branch has an open pull request"
    assert paths == []


def test_branch_pruning_runs_after_propagation_without_racing_its_push():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "safe-merged-branch-pruning.yml"
    ).read_text(encoding="utf-8")
    assert "\n  push:\n" in workflow
    assert "\n  workflow_run:\n" in workflow
    assert "Propagate Atlas Native AI across branches" in workflow
    assert "github.event.workflow_run.head_branch == 'main'" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "actions: read" in workflow
    assert "if: always()" in workflow
    assert 'if [[ "$EVENT_NAME" == "push" ]]; then' in workflow
    assert workflow.count("git diff --no-renames --name-only") == 2
    assert "gh api --paginate" in workflow
    assert "for propagation_status in queued in_progress waiting requested pending action_required stale;" in workflow
    assert "status=$propagation_status&per_page=100" in workflow
    assert "/actions/workflows/propagate-native-ai-all-branches.yml/runs?branch=main&per_page=1" in workflow
    assert "Latest Native AI propagation did not succeed; preserving every branch." in workflow
    assert "No Native AI propagation run is available as proof; preserving every branch." in workflow
    assert "Cannot prove the successful propagation covers this main commit; preserving every branch." in workflow
    assert "gh run list" not in workflow
    assert "Verify Native AI propagation is successful before pruning" in workflow
    assert "github.event_name == 'push'" in workflow
    assert "scripts/migrate_legacy_native_ai.py" in workflow
    assert ".github/workflows/propagate-native-ai-all-branches.yml" in workflow
    assert "while IFS= read -r changed_path;" in workflow
    assert 'case "$changed_path" in' in workflow
    assert "skip_pruning=false" in workflow
    assert "grep -Fxq" not in workflow, (
        "grep -q can close the pipe early under pipefail and fail open"
    )
    assert 'echo "skip=true" >> "$GITHUB_OUTPUT"' in workflow
    assert "if: steps.pruning_guard.outputs.skip != 'true'" in workflow
    # Keep the established marker guards so one-time cleanup commits cannot recurse.
    assert "[atlas-branch-prune-once-20261020]" in workflow
    assert "[atlas-branch-prune-content-once-20261020]" in workflow
    assert "[atlas-branch-prune-reviewed-once-20261020]" in workflow
    assert "[atlas-branch-prune-verified-duplicates-once-20261020]" in workflow


def test_pruning_guard_detects_renames_away_from_watched_paths(tmp_path):
    repo = tmp_path / "rename-guard-repo"
    _git(tmp_path, "init", "-b", "main", repo)
    _git(repo, "config", "user.name", "Atlas rename guard regression")
    _git(repo, "config", "user.email", "atlas-rename@example.invalid")

    watched = repo / "scripts" / "migrate_legacy_native_ai.py"
    watched.parent.mkdir(parents=True)
    watched.write_text("def migrate():\n    return 'audited'\n", encoding="utf-8")
    _git(repo, "add", "scripts/migrate_legacy_native_ai.py")
    _git(repo, "commit", "-m", "add watched migration helper")
    before = _git(repo, "rev-parse", "HEAD")

    renamed = repo / "scripts" / "legacy_migration_retired.py"
    watched.rename(renamed)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "rename watched migration helper")
    after = _git(repo, "rev-parse", "HEAD")

    changed = set(_git(repo, "diff", "--no-renames", "--name-only", before, after).splitlines())
    assert "scripts/migrate_legacy_native_ai.py" in changed
    assert "scripts/legacy_migration_retired.py" in changed


def test_pruning_push_guard_handles_large_change_list_under_pipefail(tmp_path):
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "safe-merged-branch-pruning.yml"
    ).read_text(encoding="utf-8")
    lines = workflow.splitlines()
    loop_start = next(
        index for index, line in enumerate(lines)
        if line.strip() == "while IFS= read -r changed_path; do"
    )
    loop_end = next(
        index for index in range(loop_start + 1, len(lines))
        if lines[index].strip() == 'done <<< "$changed_paths"'
    )
    loop_indent = len(lines[loop_start]) - len(lines[loop_start].lstrip())
    guarded_loop = "\n".join(
        line[loop_indent:] if line.strip() else ""
        for line in lines[loop_start:loop_end + 1]
    )

    # Execute the workflow's actual line-by-line selector against a large change
    # list. This catches grep -q early-pipe-close/SIGPIPE behavior under pipefail.
    script = (
        "set -euo pipefail\n"
        "skip_pruning=false\n"
        'changed_paths="$(printf "%s\\n" ".github/workflows/propagate-native-ai-all-branches.yml"; '
        'for index in {1..12000}; do printf "app/generated/module_%05d.py\\n" "$index"; done)"\n'
        + guarded_loop
        + '\nif [[ "$skip_pruning" == true ]]; then echo "skip=true" > "$GITHUB_OUTPUT"; '
        'else echo "skip=false" > "$GITHUB_OUTPUT"; fi\n'
    )
    output_path = tmp_path / "github-output"
    result = subprocess.run(
        ["bash", "-o", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "GITHUB_OUTPUT": str(output_path),
        },
    )
    assert result.returncode == 0, result.stderr
    assert output_path.read_text(encoding="utf-8").splitlines() == ["skip=true"]
