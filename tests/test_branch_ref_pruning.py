from __future__ import annotations

import subprocess

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
        if args[:3] == ["git", "rev-parse", "--verify"]:
            expression = args[3]
            object_ids = {
                "branch-sha:app/module.py": "blob-one",
                "main-sha:app/module.py": "blob-one",
                "branch-sha:docs/readme.md": "blob-two",
                "main-sha:docs/readme.md": "blob-two",
            }
            if expression in object_ids:
                return subprocess.CompletedProcess(args, 0, object_ids[expression] + "\n", "")
            return subprocess.CompletedProcess(
                args, 128, "", "fatal: path 'missing' does not exist in commit"
            )
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(pruning, "_run", fake_run)

    assert pruning._branch_delta_matches_main("branch-sha", "main-sha") is True


def test_branch_delta_preserves_any_changed_file_not_matching_main(monkeypatch):
    def fake_run(args, *, check=True):
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 0, "base-sha\n", "")
        if args[:5] == ["git", "diff", "--no-renames", "--name-only", "-z"]:
            return subprocess.CompletedProcess(args, 0, "app/module.py\0", "")
        if args[:3] == ["git", "rev-parse", "--verify"]:
            return subprocess.CompletedProcess(
                args, 0,
                "branch-blob\n" if args[3] == "branch-sha:app/module.py" else "main-blob\n",
                ""
            )
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(pruning, "_run", fake_run)

    assert pruning._branch_delta_matches_main("branch-sha", "main-sha") is False


def test_branch_delta_accepts_same_deleted_path_on_both_sides(monkeypatch):
    def fake_run(args, *, check=True):
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 0, "base-sha\n", "")
        if args[:5] == ["git", "diff", "--no-renames", "--name-only", "-z"]:
            return subprocess.CompletedProcess(args, 0, "old-module.py\0", "")
        if args[:3] == ["git", "rev-parse", "--verify"]:
            return subprocess.CompletedProcess(
                args, 128, "", "fatal: path 'old-module.py' does not exist in commit"
            )
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


def test_unexpected_merge_base_error_still_aborts(monkeypatch):
    def fake_run(args, *, check=True):
        return subprocess.CompletedProcess(
            args, 128, "", "fatal: repository object database corrupted"
        )

    monkeypatch.setattr(pruning, "_run", fake_run)

    with pytest.raises(RuntimeError, match="merge-base failed"):
        pruning._is_ancestor("merge-sha", "main-sha")
