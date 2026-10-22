#!/usr/bin/env python3
"""Delete only remote branch refs that are provably integrated into main.

Three proofs are accepted:
1. The exact branch tip is an ancestor of origin/main.
2. A merged PR records that exact branch tip as its head, and that PR's merge
   commit is an ancestor of origin/main (for squash/rebase merges).
3. Every path changed on the branch side since the merge base has the same
   object/content in main. This recognizes squash-merged or otherwise integrated
   changes even when commit history differs.

A historical patch-ID match alone is deliberately insufficient; if current branch
content differs from main, the branch is preserved for manual review.

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

def _object_id_at_commit(commit_sha: str, path: str) -> str | None:
    result = _run(
        ["git", "rev-parse", "--verify", f"{commit_sha}:{path}"],
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    # Missing/deleted paths are expected when comparing branch trees. Other
    # errors abort reconciliation rather than being misread as missing files.
    detail = result.stderr.strip()
    if "does not exist in" in detail or "Needed a single revision" in detail:
        return None
    raise RuntimeError(
        f"git rev-parse could not inspect path {path!r} at {commit_sha}: "
        + (detail or f"exit code {result.returncode}")
    )


def _branch_delta_mismatched_paths(branch_sha: str, main_sha: str) -> list[str]:
    """Return branch-side changed paths whose tip object differs from main."""
    merge_base_result = _run(
        ["git", "merge-base", main_sha, branch_sha],
        check=False,
    )
    if merge_base_result.returncode:
        raise RuntimeError(
            "git merge-base failed while comparing branch trees: "
            + (merge_base_result.stderr.strip() or f"exit code {merge_base_result.returncode}")
        )
    merge_base = merge_base_result.stdout.strip()
    if not merge_base:
        raise RuntimeError("git merge-base returned an empty merge base")

    diff = _run(
        ["git", "diff", "--no-renames", "--name-only", "-z", merge_base, branch_sha],
        check=False,
    )
    if diff.returncode:
        raise RuntimeError(
            "git diff failed while comparing branch trees: "
            + (diff.stderr.strip() or f"exit code {diff.returncode}")
        )

    changed_paths = [path for path in diff.stdout.split("\0") if path]
    return [
        path
        for path in changed_paths
        if _object_id_at_commit(branch_sha, path) != _object_id_at_commit(main_sha, path)
    ]


def _branch_delta_matches_main(branch_sha: str, main_sha: str) -> bool:
    """True when every branch-side path change since merge-base is already in main."""
    return not _branch_delta_mismatched_paths(branch_sha, main_sha)

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

        # Read closed PRs through the GitHub REST API. `gh pr list --state merged`
        # is not consistently supported across CLI versions; an empty result must
        # never be mistaken for evidence that no merged PR exists.
        closed_pr_result = _run(
            [
                "gh", "api", "--paginate", "--slurp",
                f"repos/{repository}/pulls?state=closed&per_page=100",
            ],
            check=False,
        )
        if closed_pr_result.returncode:
            raise RuntimeError(
                "Unable to list all closed pull requests for reconciliation: "
                + (closed_pr_result.stderr.strip() or f"exit code {closed_pr_result.returncode}")
            )
        raw_pages = json.loads(closed_pr_result.stdout)
        if not isinstance(raw_pages, list):
            raise RuntimeError("GitHub returned an unexpected closed-pull-request response")
        if raw_pages and isinstance(raw_pages[0], list):
            closed_prs = [item for page in raw_pages for item in page]
        else:
            closed_prs = raw_pages
        merged_prs = [
            {
                "headRefName": item.get("head", {}).get("ref"),
                "headRefOid": item.get("head", {}).get("sha"),
                "mergeCommit": {"oid": item.get("merge_commit_sha")},
            }
            for item in closed_prs
            if isinstance(item, dict)
            and item.get("merged_at")
            and isinstance(item.get("head"), dict)
        ]

        deleted = 0
        preserved = 0
        failed = 0
        decisions: list[tuple[str, str, str]] = []

        def record(status: str, branch_name: str, reason: str) -> None:
            decisions.append((status, branch_name, reason))
            print(f"{status} {branch_name}: {reason}")
        for branch_name in branches:
            if branch_name in open_pr_heads:
                record("PRESERVE", branch_name, "branch has an open pull request")
                preserved += 1
                continue

            ref = f"refs/remotes/origin/{branch_name}"
            result = _run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], check=False)
            if result.returncode:
                record("PRESERVE", branch_name, "remote ref changed or could not be resolved")
                preserved += 1
                continue
            branch_sha = result.stdout.strip()

            if _is_ancestor(branch_sha, main_sha):
                safe = True
                reason = "exact branch tip is an ancestor of main"
            else:
                branch_merged_prs = [
                    item for item in merged_prs
                    if item.get("headRefName") == branch_name
                ]
                safe, reason = should_delete_ref(
                    branch_name,
                    branch_sha,
                    main_sha,
                    open_pr_heads,
                    branch_merged_prs,
                    _is_ancestor,
                )
                if not safe and _branch_delta_matches_main(branch_sha, main_sha):
                    safe = True
                    reason = "all branch-side changed paths match main"

            if not safe:
                mismatch_paths = _branch_delta_mismatched_paths(branch_sha, main_sha)
                if mismatch_paths:
                    sample = ", ".join(mismatch_paths[:8])
                    remainder = f" (+{len(mismatch_paths) - 8} more)" if len(mismatch_paths) > 8 else ""
                    reason = (
                        f"{reason}; {len(mismatch_paths)} branch-side path(s) differ from main: "
                        f"{sample}{remainder}"
                    )
                record("PRESERVE", branch_name, reason)
                preserved += 1
                continue

            # Re-read the live ref immediately before deletion. The GitHub ref-delete
            # endpoint has no expected-SHA parameter, so a moved ref is always preserved.
            live = _run(
                ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch_name}"],
                check=False,
            )
            if live.returncode or not live.stdout.strip():
                record("PRESERVE", branch_name, "ref disappeared or could not be re-read")
                preserved += 1
                continue
            live_sha = live.stdout.split()[0]
            if live_sha != branch_sha:
                record("PRESERVE", branch_name, "branch tip changed during reconciliation")
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
                record("PRESERVE", branch_name, "could not re-check open pull requests")
                preserved += 1
                continue
            try:
                opened = json.loads(latest_open.stdout)
            except json.JSONDecodeError:
                record("PRESERVE", branch_name, "malformed open-pull-request response")
                preserved += 1
                continue
            if opened:
                record("PRESERVE", branch_name, "an open pull request appeared during reconciliation")
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
                record(
                    "ERROR",
                    branch_name,
                    "safe-to-delete proof passed but GitHub ref deletion failed: "
                    + (deletion.stderr.strip() or f"exit code {deletion.returncode}"),
                )
                failed += 1
                continue
            record("DELETED", branch_name, reason)
            deleted += 1

        print(f"Branch prune summary: deleted={deleted}, preserved={preserved}, failed={failed}")
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as summary:
                summary.write("# Atlas-Bot branch reconciliation\n\n")
                summary.write(
                    f"Main SHA: `{main_sha}` — deleted **{deleted}**, "
                    f"preserved **{preserved}**, failed **{failed}**.\n\n"
                )
                summary.write("| Branch | Decision | Evidence / reason |\n|---|---|---|\n")
                for decision, branch_name, reason in decisions:
                    safe_branch = branch_name.replace("|", "\\|")
                    safe_reason = reason.replace("|", "\\|").replace("\n", " ")
                    summary.write(f"| `{safe_branch}` | {decision} | {safe_reason} |\n")
        return 1 if failed else 0
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"BLOCKED: safe branch pruning aborted: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
