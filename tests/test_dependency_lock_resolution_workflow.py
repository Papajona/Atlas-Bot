from __future__ import annotations

from pathlib import Path


def test_dependency_lock_resolution_is_artifact_only_and_cannot_push_unverified_pr_heads():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "dependency-lock-resolution.yml"
    ).read_text(encoding="utf-8")

    # Generated locks remain downloadable for review, but commits must be made by
    # the PR author so normal pull_request checks run on the exact resulting SHA.
    assert "resolve-hash-locks:" in workflow
    assert "Publish verified lock candidates and checksum" in workflow
    assert "atlas-python-hash-lock-candidates" in workflow
    assert "sha256sum --check requirements-locks.sha256" in workflow

    # There must be no privileged second job that can push with GITHUB_TOKEN,
    # skip subsequent PR checks, or race a stale artifact onto a newer branch tip.
    assert "commit-verified-locks:" not in workflow
    assert "contents: write" not in workflow
    assert "github.token" not in workflow
    assert "git push" not in workflow
    assert "persist-credentials: true" not in workflow


def test_dependency_lock_candidates_are_hash_installed_before_publication():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "dependency-lock-resolution.yml"
    ).read_text(encoding="utf-8")

    runtime_install = workflow.index(
        'python -m pip install --no-binary=multidict --require-hashes -r "$RUNNER_TEMP/requirements.lock"'
    )
    dev_install = workflow.index(
        'python -m pip install --no-binary=multidict --require-hashes -r "$RUNNER_TEMP/requirements-dev.lock"'
    )
    publish = workflow.index("Publish verified lock candidates and checksum")
    assert runtime_install < publish
    assert dev_install < publish
