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

    runtime_patterns = {
        line for line in runtime_ignore
        if line and not line.startswith("#")
    }
    verification_patterns = {
        line for line in verification_ignore
        if line and not line.startswith("#") and line not in {"!tests/", "!tests/**"}
    }
    assert runtime_patterns == verification_patterns
    assert "COPY tests ./tests" not in runtime_dockerfile
    assert "COPY tests ./tests" in verification_dockerfile
