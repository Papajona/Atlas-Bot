"""Fail-closed validation for optional Groq deployment configuration."""
from __future__ import annotations

import re
import sys

_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")
_TOKEN_PATTERN = re.compile(r"[1-9][0-9]{0,4}\Z")


def validate_groq_deploy_env(model: str, max_completion_tokens: str, enabled: str) -> tuple[str, int, bool]:
    """Validate deployment values before they are interpolated into gcloud flags."""
    if not isinstance(model, str) or not _MODEL_PATTERN.fullmatch(model):
        raise ValueError("GROQ_MODEL must be a safe model identifier (letters, digits, . _ : / -).")
    if not isinstance(max_completion_tokens, str) or not _TOKEN_PATTERN.fullmatch(max_completion_tokens):
        raise ValueError("GROQ_MAX_COMPLETION_TOKENS must be a positive decimal integer no greater than 8192.")
    token_count = int(max_completion_tokens)
    if not 1 <= token_count <= 8192:
        raise ValueError("GROQ_MAX_COMPLETION_TOKENS must be between 1 and 8192.")
    normalized_enabled = enabled.strip().lower() if isinstance(enabled, str) else ""
    if normalized_enabled not in {"true", "false"}:
        raise ValueError("GROQ_RESEARCH_REVIEW_ENABLED must be true or false.")
    return model, token_count, normalized_enabled == "true"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        print("Usage: validate_groq_deploy_env.py MODEL MAX_COMPLETION_TOKENS ENABLED", file=sys.stderr)
        return 2
    try:
        validate_groq_deploy_env(*args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
