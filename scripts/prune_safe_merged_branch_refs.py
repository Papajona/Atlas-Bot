#!/usr/bin/env python3
"""Delete only remote branch refs that are provably integrated into main.

Three independent proofs are accepted:
1. The exact branch tip is an ancestor of origin/main.
2. A merged PR records that exact branch tip as its head, and that PR's merge
   commit is an ancestor of origin/main (for squash/rebase merges).
3. With no branch-only merge commits, every branch-only commit has a matching
   patch in main according to git cherry (no unique '+' patches).

Any open-PR branch, unknown branch history, API failure, or head moved after a
merge is preserved. This script never deletes main.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Any


def should_delete_ref(
    branch_name: str,
    branch_sha: str,
    main_sha: str,
    open_pr_heads: set[str],
    merged_prs: Iterable[Mapping[str, Any]],
    is_ancestor: Callable[[str, str], bool],
) -> tuple[bool, str]:
    """Return deletion decision only when the exact ref has integration proof."""
    if branch_name == "main":
        return False, "protected main ref"
    if branch_name in open_pr_heads:
        return False, "branch has an open pull request"
    if is_ancestor(branch_sha, main_sha):
        return True, "exact branch tip is an ancestor of main"

    for pr in merged_prs:
        if pr.get("headRefName") != branch_name or pr.get("headRefOid") != branch_sha:
            continue
        merge_commit = pr.get("mergeCommit")
        merge_sha = merge_commit.get("oid") if isinstance(merge_commit, Mapping) else None
        if merge_sha and is_ancestor(str(merge_sha), main_sha):
            return True, f"exact branch tip was merged via commit {merge_sha} on main"
    return False, "no exact integration proof; preserve branch"


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"Command failed ({' '.join(args[:4])}): {detail}")
    return result


def _is_ancestor(ancestor_sha: str, descendant_sha: str) -> bool:
    result = _run(
        ["git", "merge-base", "--is-ancestor", ancestor_sha, descendant_sha],
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise RuntimeError(
        "git merge-base failed while checking branch integration: "
        + (result.stderr.strip() or f"exit code {result.returncode}")
    )

def _all_branch_commits_are_patch_equivalent(branch_sha: str, main_sha: str) -> bool:
    """Accept patch-equivalence only when no branch-only merge commits exist.

    `git cherry` marks patch-equivalent branch commits with '-' and unique
    patches with '+'. Merge-resolution commits are excluded from that comparison,
    so any merge commit reachable only from this branch causes conservative
    preservation.
    """
    merges = _run(
        ["git", "rev-list", "--merges", f"{main_sha}..{branch_sha}"],
        check=False,
    )
    if merges.returncode:
        raise RuntimeError(
            "git rev-list failed while checking branch merge commits: "
            + (merges.stderr.strip() or f"exit code {merges.returncode}")
        )
    if merges.stdout.strip():
        return False

    cherry = _run(["git", "cherry", main_sha, branch_sha], check=False)
    if cherry.returncode:
        raise RuntimeError(
            "git cherry failed while comparing branch patches: "
            + (cherry.stderr.strip() or f"exit code {cherry.returncode}")
        )
    marks = [line[0] for line in cherry.stdout.splitlines() if line and line[0] in "+-"]
    return bool(marks) and all(mark == "-" for mark in marks)


def main() -> int:
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repository or "/" not in repository:
        print("BLOCKED: GITHUB_REPOSITORY must be owner/repository", file=sys.stderr)
        return 2
    if not os.environ.get("GH_TOKEN"):
        print("BLOCKED: GH_TOKEN is required to inspect and safely delete refs", file=sys.stderr)
        return 2

    try:
        _run(["git", "fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"])
        main_sha = _run(
            ["git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"]
        ).stdout.strip()
        branch_output = _run(
            ["git", "for-each-ref", "--format=%(refname:strip=3)", "refs/remotes/origin"]
        ).stdout
        branches = sorted(
            {
                item.strip()
                for item in branch_output.splitlines()
                if item.strip() and item.strip() not in {"HEAD", "main"}
            }
        )

        open_pr_payload = _run(
            [
                "gh", "pr", "list", "--repo", repository, "--state", "open",
                "--limit", "1000", "--json", "headRefName",
            ]
        ).stdout
        open_pr_heads = {
            str(item.get("headRefName") or "")
            for item in json.loads(open_pr_payload)
            if item.get("headRefName")
        }

        deleted = 0
        preserved = 0
        failed = 0
        for branch_name in branches:
            if branch_name in open_pr_heads:
                print(f"PRESERVE {branch_name}: branch has an open pull request")
                preserved += 1
                continue

            ref = f"refs/remotes/origin/{branch_name}"
            result = _run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], check=False)
            if result.returncode:
                print(f"PRESERVE {branch_name}: remote ref changed or could not be resolved")
                preserved += 1
                continue
            branch_sha = result.stdout.strip()

            if _is_ancestor(branch_sha, main_sha):
                safe = True
                reason = "exact branch tip is an ancestor of main"
            else:
                payload = _run(
                    [
                        "gh", "pr", "list", "--repo", repository, "--state", "merged",
                        "--head", f"{repository.split('/', 1)[0]}:{branch_name}", "--limit", "1000",
                        "--json", "headRefName,headRefOid,mergeCommit",
                    ]
                ).stdout
                merged_prs = json.loads(payload)
                safe, reason = should_delete_ref(
                    branch_name,
                    branch_sha,
                    main_sha,
                    open_pr_heads,
                    merged_prs,
                    _is_ancestor,
                )
                if not safe and _all_branch_commits_are_patch_equivalent(branch_sha, main_sha):
                    safe = True
                    reason = "all non-merge branch commits are patch-equivalent to main"

            if not safe:
                print(f"PRESERVE {branch_name}: {reason}")
                preserved += 1
                continue

            # Re-read the live ref immediately before deletion. The GitHub ref-delete
            # endpoint has no expected-SHA parameter, so a moved ref is always preserved.
            live = _run(
                ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch_name}"],
                check=False,
            )
            if live.returncode or not live.stdout.strip():
                print(f"PRESERVE {branch_name}: ref disappeared or could not be re-read")
                preserved += 1
                continue
            live_sha = live.stdout.split()[0]
            if live_sha != branch_sha:
                print(f"PRESERVE {branch_name}: branch tip changed during reconciliation")
                preserved += 1
                continue

            latest_open = _run(
                [
                    "gh", "pr", "list", "--repo", repository, "--state", "open",
                    "--head", f"{repository.split('/', 1)[0]}:{branch_name}",
                    "--limit", "1000", "--json", "number",
                ],
                check=False,
            )
            if latest_open.returncode:
                print(f"PRESERVE {branch_name}: could not re-check open pull requests")
                preserved += 1
                continue
            try:
                opened = json.loads(latest_open.stdout)
            except json.JSONDecodeError:
                print(f"PRESERVE {branch_name}: malformed open-pull-request response")
                preserved += 1
                continue
            if opened:
                print(f"PRESERVE {branch_name}: an open pull request appeared during reconciliation")
                preserved += 1
                continue

            deletion = _run(
                [
                    "gh", "api", "--method", "DELETE",
                    f"repos/{repository}/git/refs/heads/{branch_name}",
                ],
                check=False,
            )
            if deletion.returncode:
                print(
                    f"ERROR {branch_name}: safe-to-delete proof passed but GitHub ref deletion failed: "
                    + (deletion.stderr.strip() or f"exit code {deletion.returncode}"),
                    file=sys.stderr,
                )
                failed += 1
                continue
            print(f"DELETED {branch_name}: {reason}")
            deleted += 1

        print(f"Branch prune summary: deleted={deleted}, preserved={preserved}, failed={failed}")
        return 1 if failed else 0
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"BLOCKED: safe branch pruning aborted: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
