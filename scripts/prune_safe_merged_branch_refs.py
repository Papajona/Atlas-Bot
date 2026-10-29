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

    detail = result.stderr.strip()
    requested = ancestor_sha.lower()
    diagnostic_lines = [line.strip().lower() for line in detail.splitlines() if line.strip()]

    # Only tolerate a narrowly recognized diagnostic that names the exact
    # requested historical commit as unavailable. Mixed diagnostics, including
    # any object database/read/corruption error, must stop reconciliation.
    storage_fault_markers = (
        "object database",
        "corrupt",
        "unable to read",
        "failed to read",
        "packfile",
        "loose object",
        "missing object",
        "hash mismatch",
        "object file",
        "invalid object",
    )
    has_storage_fault = any(marker in detail.lower() for marker in storage_fault_markers)
    known_missing_lines = {
        (f"fatal: not a valid commit name {requested}",),
        (f"fatal: bad object {requested}",),
        (
            f"fatal: ambiguous argument '{requested}': unknown revision or path not in the working tree.",
        ),
        (
            f"fatal: ambiguous argument '{requested}': unknown revision or path not in the working tree.",
            "use '--' to separate paths from revisions, like this:",
            "'git <command> [<revision>...] -- [<file>...]'",
        ),
    }
    if not has_storage_fault and tuple(diagnostic_lines) in known_missing_lines:
        return False

    raise RuntimeError(
        "git merge-base failed while checking branch integration: "
        + (detail or f"exit code {result.returncode}")
    )


def _tree_entry_at_commit(
    commit_sha: str, path: str
) -> tuple[str, str, str] | None:
    """Return the exact path's Git mode, object type, and object ID, or None if absent."""
    result = _run(
        ["git", "--literal-pathspecs", "ls-tree", "-z", "-tr", "--full-tree", commit_sha, "--", path],
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"git ls-tree could not inspect path {path!r} at {commit_sha}: "
            + (result.stderr.strip() or f"exit code {result.returncode}")
        )

    exact_entries = []
    for entry in (entry for entry in result.stdout.split("\0") if entry):
        metadata, separator, returned_path = entry.partition("\t")
        if not separator:
            raise RuntimeError(
                f"git ls-tree returned malformed output for path {path!r} at {commit_sha}"
            )
        # Recursive lookup may include the requested tree and its descendants.
        # Only the exact path describes the object being reconciled.
        if returned_path == path:
            exact_entries.append(metadata)
    if not exact_entries:
        return None
    if len(exact_entries) != 1:
        raise RuntimeError(
            f"git ls-tree returned duplicate exact entries for path {path!r} at {commit_sha}"
        )
    fields = exact_entries[0].split()
    if len(fields) != 3:
        raise RuntimeError(
            f"git ls-tree returned an unexpected tree entry for path {path!r} at {commit_sha}"
        )
    mode, object_type, object_id = fields
    return mode, object_type, object_id


def _reconcile_unknown_protection_branches(
    branches: list[str], branch_protection: dict[str, bool]
) -> list[str]:
    """Discard only stale refs whose absence was verified against origin."""
    remaining = list(branches)
    unknown_protection = sorted(set(remaining).difference(branch_protection))
    for name in unknown_protection:
        remote = _run(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{name}"],
            check=False,
        )
        if remote.returncode != 0:
            detail = remote.stderr.strip() or f"exit code {remote.returncode}"
            raise RuntimeError(
                f"Unable to read remote status for branch {name!r}; refusing pruning: {detail}"
            )
        if remote.stdout.strip():
            raise RuntimeError(
                f"GitHub did not return protection status for branch {name!r}, "
                "and the branch still exists remotely; refusing pruning."
            )
        remaining.remove(name)
        print(f"ALREADY ABSENT (remote ref verified missing): {name}")
    return remaining


def _branch_delta_mismatched_paths(branch_sha: str, main_sha: str) -> list[str]:
    """Return branch-side changed paths whose complete tree entries differ from main."""
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
        if _tree_entry_at_commit(branch_sha, path) != _tree_entry_at_commit(main_sha, path)
    ]


def _branch_delta_matches_main(branch_sha: str, main_sha: str) -> bool:
    """True when every branch-side path change since merge-base is already in main."""
    return not _branch_delta_mismatched_paths(branch_sha, main_sha)


_SUPERSEDED_ANDROID_RELEASE_BRANCHES = {
    # Exact historical branch tips; never match a branch name alone.
    "fix/android-release-trigger-gating-20261023":
        "fc84eac73078c65a8c253101c40e963958b74e60",
    "fix/android-release-trigger-gating-followup-20261023":
        "6b9feef6b55be4ef06eef630d4b558e0ffad35b7",
}
_ANDROID_RELEASE_GATE_PATHS = {
    ".github/workflows/android-release.yml",
    "tests/test_android_production_backend_wiring.py",
}

# Exact old branch tips whose only branch-side changes were superseded Native AI
# migration machinery. Names alone are never sufficient: both the SHA and the
# complete branch-side path allowlist must match before a ref can be retired.
_SUPERSEDED_NATIVE_AI_MIGRATION_BRANCHES = {
    "fix/native-ai-branch-propagation-strategy-preservation-20261027": "1fce3784f16d36865206af3ea01601f754000156",
    "fix/native-ai-propagation-error-alias-20261027": "a7f35abfe8d3a73af329c5ae34320afe5f0def12",
    "fix/native-ai-propagation-error-alias-verified-20261027": "b933822d8a92d197e04b923068e9b245239ed961",
    "fix/native-ai-propagation-final-20261027": "1ae360c3df0b1673c5ca01a807c2708d1881220a",
    "fix/native-ai-propagation-final-reviewed-20261027": "1bcb94a634262411de22550f9110e8feef61a8b1",
    "fix/native-ai-propagation-import-safety-20261027": "10dbbfeb5568f95fcd0f3a96f3268891c37c7604",
    "fix/native-ai-propagation-import-safety-verified-20261027": "a08931e0c9777e05b12b796fdc75669f40004e1b",
    "fix/native-ai-propagation-qodo-final2-20261027": "cdc1320014c9d284e2ec51e426bdf7798ba97ce6",
    "fix/native-ai-propagation-qodo-final-20261027": "ab2b5c0cf46b4146beec300ecb013942b2ce8633",
    "fix/native-ai-propagation-verified-20261027": "a7f35abfe8d3a73af329c5ae34320afe5f0def12",
    "fix/native-ai-propagation-verified-final-20261027": "de24e7a1eb1a0bb4c06de1b375895ca0ee9c78f7",
    "fix/native-ai-semantic-branch-migration-20261027": "015f1ed751d7ae6590dc45be38cb599a3ecef67e",
    "fix/qodo-native-ai-binding-analysis-20261028": "9f6d5f43863270be9155fdaf1566fde654a4486d",
    "fix/remediation-2f1bec4a-267583": "2da14cb8c9fd15824750cea905eeab9dad6243d8",
    "fix/remediation-3e97e442-fa299f": "b4a3a113aa47ad27933f2f1f2aa9d0f04a01d750",
    "fix/remediation-5bad2a5b-c2b709": "c062f6c0f9f7332aaaaea58c6544d6cd20d95acf",
    "fix/remediation-29b9a458-8b7996": "53047279afc33ca9bc9a1e833d4b90664bfcf942",
    "fix/remediation-92aa0059-36b9da": "7e50fdb5e6e3eac9d17b4afa9a1f0cf4117584e2",
    "fix/remediation-585ba76d-28013b": "c0ca34e115bd70e1c96c4dfe77183440a68ee25c",
    "fix/remediation-e28b5315-d3bb64": "9784f1e82ba6a7823872f600e6d33226e43d97c9",
    "fix/remediation-fa13ad7e-7493b5": "3a78cc2a39844eb92227bd9b20d2d104f704931f",
}
_NATIVE_AI_MIGRATION_ARTIFACT_PATHS = {
    ".github/workflows/propagate-native-ai-all-branches.yml",
    "scripts/migrate_legacy_native_ai.py",
    "tests/test_legacy_native_ai_migration.py",
    "tests/test_native_ai_branch_propagation_workflow.py",
}


def _git_file_text_at_commit(commit_sha: str, path: str) -> str:
    result = _run(["git", "show", f"{commit_sha}:{path}"], check=False)
    if result.returncode:
        raise RuntimeError(
            f"Cannot verify superseding Android release gate at {commit_sha}:{path}: "
            + (result.stderr.strip() or f"exit code {result.returncode}")
        )
    return result.stdout


def _main_has_explicit_android_release_gate(main_sha: str) -> bool:
    """Require both production workflow restrictions and the committed regression test."""
    workflow = _git_file_text_at_commit(
        main_sha, ".github/workflows/android-release.yml"
    )
    test_source = _git_file_text_at_commit(
        main_sha, "tests/test_android_production_backend_wiring.py"
    )
    if "\n  push:\n" not in workflow or "\n  android-release:\n" not in workflow:
        return False
    push_trigger = workflow.split("\n  push:\n", 1)[1].split(
        "\npermissions:\n", 1
    )[0]
    release_job = workflow.split("  android-release:\n", 1)[1].split(
        "\n    runs-on:", 1
    )[0]
    expected_release_condition = (
        "(github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main') || "
        "(github.event_name == 'push' && startsWith(github.ref, 'refs/tags/android-v'))"
    )
    release_conditions = [
        " ".join(line.split("if:", 1)[1].split())
        for line in release_job.splitlines()
        if line.startswith("    if:")
    ]
    return (
        'tags:\n      - "android-v*"' in push_trigger
        and "branches:" not in push_trigger
        and "paths:" not in push_trigger
        # Require the whole job condition, not merely the presence of two safe
        # alternatives. Extra alternatives (for example pull_request) can bypass
        # the release boundary even when the required substrings are present.
        and release_conditions == [expected_release_condition]
        and "test_signed_release_is_only_triggered_by_an_explicit_release_tag_or_manual_main_dispatch"
        in test_source
    )


def _is_explicitly_superseded_android_release_ref(
    branch_name: str,
    branch_sha: str,
    main_sha: str,
    mismatch_paths: list[str],
) -> bool:
    """Allow only two exact, audited Android branches after main proves the stronger gate."""
    expected_sha = _SUPERSEDED_ANDROID_RELEASE_BRANCHES.get(branch_name)
    if expected_sha is None or branch_sha != expected_sha:
        return False
    if not mismatch_paths or not set(mismatch_paths).issubset(_ANDROID_RELEASE_GATE_PATHS):
        return False
    return _main_has_explicit_android_release_gate(main_sha)


def _main_has_verified_native_ai_migration(main_sha: str) -> bool:
    """Require main to contain the tested fail-closed migration and duplicate guards."""
    migration = _git_file_text_at_commit(main_sha, "scripts/migrate_legacy_native_ai.py")
    tests = _git_file_text_at_commit(main_sha, "tests/test_legacy_native_ai_migration.py")
    return (
        "def _is_replaceable_legacy_market_context(" in migration
        and "def _safe_canonical_trade_safety_validator(" in migration
        and "test_strategy_migration_accepts_recognized_two_and_three_provider_flows" in tests
        and "test_equivalent_legacy_trade_safety_validator_is_deduplicated" in tests
        and "test_ai_provider_migration_accepts_only_equivalent_trade_safety_validator" in tests
    )


def _is_explicitly_superseded_native_ai_migration_ref(
    branch_name: str,
    branch_sha: str,
    main_sha: str,
    mismatch_paths: list[str],
) -> bool:
    """Retire only exact legacy migration-only tips after main has its verified replacement."""
    expected_sha = _SUPERSEDED_NATIVE_AI_MIGRATION_BRANCHES.get(branch_name)
    if expected_sha is None or branch_sha != expected_sha:
        return False
    if not mismatch_paths or not set(mismatch_paths).issubset(_NATIVE_AI_MIGRATION_ARTIFACT_PATHS):
        return False
    return _main_has_verified_native_ai_migration(main_sha)


def _resolve_branch_integration(
    branch_name: str,
    branch_sha: str,
    main_sha: str,
    open_pr_heads: set[str],
    merged_prs: Iterable[Mapping[str, Any]],
) -> tuple[bool, str, list[str]]:
    """Resolve deletion proof and compute tree mismatches at most once."""
    safe, reason = should_delete_ref(
        branch_name,
        branch_sha,
        main_sha,
        open_pr_heads,
        merged_prs,
        _is_ancestor,
    )
    if safe or reason != "no exact integration proof; preserve branch":
        return safe, reason, []

    mismatch_paths = _branch_delta_mismatched_paths(branch_sha, main_sha)
    if not mismatch_paths:
        return True, "all branch-side changed paths match main", []
    if _is_explicitly_superseded_android_release_ref(
        branch_name, branch_sha, main_sha, mismatch_paths
    ):
        return (
            True,
            "exact historical Android release-trigger branch superseded by main's "
            "verified tag/manual-dispatch-only release gate",
            [],
        )
    if _is_explicitly_superseded_native_ai_migration_ref(
        branch_name, branch_sha, main_sha, mismatch_paths
    ):
        return (
            True,
            "exact historical Native AI migration-only branch superseded by main's "
            "tested fail-closed migration contract",
            [],
        )
    return False, reason, mismatch_paths


def _load_branch_protection_status(repository: str) -> dict[str, bool]:
    """Load complete GitHub branch protection status or abort without deleting refs."""
    result = _run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repository}/branches?per_page=100",
            "--jq",
            ".[] | [.name, (.protected | tostring)] | @tsv",
        ],
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "Unable to determine GitHub branch protection status; refusing all deletions: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}")
        )

    statuses: dict[str, bool] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 2 or not fields[0] or fields[1] not in {"true", "false"}:
            raise RuntimeError(
                "GitHub returned malformed branch protection status; refusing all deletions"
            )
        name, status = fields
        if name in statuses and statuses[name] != (status == "true"):
            raise RuntimeError(
                f"Conflicting GitHub branch protection status for {name!r}; refusing all deletions"
            )
        statuses[name] = status == "true"

    if not statuses or "main" not in statuses:
        raise RuntimeError(
            "GitHub branch-protection enumeration was empty or omitted main; refusing all deletions"
        )
    return statuses


def _delete_remote_ref_with_lease(branch_name: str, expected_sha: str) -> tuple[str, str]:
    """Delete a remote branch only if its current tip still equals the proven SHA.

    Return (status, reason), where status is DELETED, PRESERVE, or ERROR.
    A stale lease is a preservation outcome; other push errors remain failures.
    """
    result = _run(
        [
            "git",
            "push",
            "origin",
            f"--force-with-lease=refs/heads/{branch_name}:{expected_sha}",
            f":refs/heads/{branch_name}",
        ],
        check=False,
    )
    output = "\n".join(
        part.strip() for part in (result.stderr, result.stdout) if part and part.strip()
    )
    if result.returncode == 0:
        return "DELETED", "conditional deletion succeeded with an explicit expected-SHA lease"

    # Another cleanup may have removed the ref after enumeration. A successful
    # read-back proving absence means the desired state already holds; report a
    # safe no-op, not a job failure.
    remote_ref = _run(
        ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch_name}"],
        check=False,
    )
    if remote_ref.returncode == 0 and not remote_ref.stdout.strip():
        return (
            "PRESERVE",
            "remote ref was already removed by another cleanup; treating deletion as a successful no-op",
        )

    lowered = output.lower()
    if "stale info" in lowered or (
        "cannot lock ref" in lowered
        and ("expected" in lowered or "but is at" in lowered)
    ):
        return (
            "PRESERVE",
            "conditional deletion lease was rejected because the remote ref changed; branch preserved",
        )

    detail = output or f"exit code {result.returncode}"
    return "ERROR", f"conditional branch deletion failed: {detail}"


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

        branch_protection = _load_branch_protection_status(repository)
        branches = _reconcile_unknown_protection_branches(branches, branch_protection)

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
            if branch_protection[branch_name]:
                record(
                    "PRESERVE",
                    branch_name,
                    "GitHub marks this branch protected; deletion was not attempted",
                )
                preserved += 1
                continue

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

            branch_merged_prs = [
                item for item in merged_prs
                if item.get("headRefName") == branch_name
            ]
            safe, reason, mismatch_paths = _resolve_branch_integration(
                branch_name,
                branch_sha,
                main_sha,
                open_pr_heads,
                branch_merged_prs,
            )

            if not safe:
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

            # Re-read the live ref immediately before deletion. The subsequent
            # force-with-lease push atomically refuses to delete a moved ref.
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

            # The earlier ls-remote check is diagnostic only. The explicit
            # force-with-lease on this push is the atomic guard against a ref
            # update between proof and deletion.
            deletion_status, deletion_reason = _delete_remote_ref_with_lease(
                branch_name, branch_sha
            )
            if deletion_status == "PRESERVE":
                record("PRESERVE", branch_name, deletion_reason)
                preserved += 1
                continue
            if deletion_status == "ERROR":
                record("ERROR", branch_name, deletion_reason)
                failed += 1
                continue
            record("DELETED", branch_name, f"{reason}; {deletion_reason}")
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
