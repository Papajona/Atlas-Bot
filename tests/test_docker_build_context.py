from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_image_excludes_tests_but_verification_image_includes_them():
    runtime_ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    verification_ignore = (
        ROOT / "Dockerfile.verify.dockerignore"
    ).read_text(encoding="utf-8").splitlines()
    runtime_dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    verification_dockerfile = (ROOT / "Dockerfile.verify").read_text(encoding="utf-8")

    assert "tests/" in runtime_ignore
    assert "!tests/" not in runtime_ignore
    assert "!tests/**" not in runtime_ignore

    assert "tests/" in verification_ignore
    assert "!tests/" in verification_ignore
    assert "!tests/**" in verification_ignore
    # Docker ignore exceptions are order-sensitive: both must follow the tests/
    # exclusion or Docker will silently omit the verification test sources.
    tests_exclusion_index = verification_ignore.index("tests/")
    assert verification_ignore.index("!tests/") > tests_exclusion_index
    assert verification_ignore.index("!tests/**") > tests_exclusion_index

    # Verification tests inspect workflow/governance evidence that the runtime
    # image intentionally excludes; keep those paths in the verification context.
    assert ".github" not in verification_ignore
    assert "docs/" not in verification_ignore
    runtime_patterns = {
        line for line in runtime_ignore
        if line and not line.startswith("#")
    }
    verification_patterns = {
        line for line in verification_ignore
        if line and not line.startswith("#") and line not in {"!tests/", "!tests/**"}
    }
    assert verification_patterns == runtime_patterns - {".github", "docs/"}
    assert "COPY tests ./tests" not in runtime_dockerfile
    assert "COPY tests ./tests" in verification_dockerfile
