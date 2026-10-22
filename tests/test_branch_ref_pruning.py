from __future__ import annotations

import subprocess

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


def test_patch_equivalence_only_prunes_when_all_branch_commits_have_matches(monkeypatch):
    def fake_run(args, *, check=True):
        if args[:3] == ["git", "rev-list", "--merges"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["git", "cherry"]:
            return subprocess.CompletedProcess(args, 0, "- abc123\n- def456\n", "")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(pruning, "_run", fake_run)

    assert pruning._all_branch_commits_are_patch_equivalent("branch", "main") is True


def test_patch_equivalence_preserves_any_branch_unique_patch(monkeypatch):
    def fake_run(args, *, check=True):
        if args[:3] == ["git", "rev-list", "--merges"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["git", "cherry"]:
            return subprocess.CompletedProcess(args, 0, "- abc123\n+ unique456\n", "")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(pruning, "_run", fake_run)

    assert pruning._all_branch_commits_are_patch_equivalent("branch", "main") is False


def test_patch_equivalence_preserves_branch_only_merge_commits(monkeypatch):
    def fake_run(args, *, check=True):
        if args[:3] == ["git", "rev-list", "--merges"]:
            return subprocess.CompletedProcess(args, 0, "merge123\n", "")
        raise AssertionError("git cherry must not run when branch-only merges exist")

    monkeypatch.setattr(pruning, "_run", fake_run)

    assert pruning._all_branch_commits_are_patch_equivalent("branch", "main") is False


def test_patch_equivalence_does_not_delete_when_git_cherry_has_no_commits(monkeypatch):
    def fake_run(args, *, check=True):
        if args[:3] == ["git", "rev-list", "--merges"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["git", "cherry"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(pruning, "_run", fake_run)

    assert pruning._all_branch_commits_are_patch_equivalent("branch", "main") is False
