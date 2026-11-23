#!/usr/bin/env python3
"""Repository secret/configuration scanner used by CI and release verification.

This is intentionally conservative: it reports high-signal credential assignments and
private-key/seed material while allowing placeholders and public-key material.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", "dist", "build", ".pytest_cache"}
# Include source, documentation, Android resources, deployment/configuration files and
# common text-based key/certificate containers. An allowlist that omits Markdown or
# PEM/key extensions lets credentials bypass the same scanner CI uses as a release gate.
TEXT_SUFFIXES = {
    ".py", ".sh", ".bash", ".env", ".example", ".yml", ".yaml", ".json",
    ".toml", ".ini", ".gradle", ".kts", ".md", ".txt", ".xml", ".properties",
    ".java", ".kt", ".js", ".html", ".css", ".sql", ".pem", ".key", ".crt",
    ".conf", ".cfg", ".lock", ".patch",
}
ASSIGNMENT = re.compile(r"""(?i)(?<![A-Za-z0-9_])["']?(?P<key>(?:[A-Za-z_][A-Za-z0-9_]*_)?(?:api[_-]?key|api[_-]?secret|secret[_-]?key|private[_-]?key|encryption[_-]?key|signing[_-]?key|service[_-]?role[_-]?key|password|token|mnemonic|seed[_-]?phrase))["']?\s*[:=]\s*["']?(?P<value>[A-Za-z0-9_+/=.-]{20,})""")
SOURCE_CODE_SUFFIXES = {".py", ".js", ".java", ".kt", ".sh", ".bash"}
HELPER_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PRIVATE_KEY = re.compile(r"^\s*-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----\s*$")
TRON_SEED = re.compile(r"""(?i)^\s*(?:-\s*)?(?:export\s+)?["']?(?:mnemonic|seed[_-]?phrase|private[_-]?key)["']?\s*[:=]\s*(?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^#\r\n]+)""")

PLACEHOLDERS = {
    "changeme", "change-me", "example", "placeholder", "your-secret", "your-key",
    "test-password", "verification-password", "verification", "atlas-ci", "fake-key",
    "generate_a_fernet_key_and_store_in_secret_manager", "fixture", "ci-only",
    "paper_test_password", "test_password", "distinct-paper-secret",
    "different-primary-secret", "legacy-secret", "supported-review-key",
    "legacy-hosted-provider", "must-not-become-a-helper-setting", "must-not-be-persisted",
    # Exact values used only by repository verification fixtures.
    "ci-only-keystore-password",
    "ci-only-key-password",
    "test-key-not-a-real-secret",
    "test-admin-token-for-ci",
    "ci-paper-smoke-placeholder",
    "verification_password",
    "atlas-ci-production-verification-secret-32-bytes",
    "verification-model-private-key",
    "production-verification-only",
    "server-refresh-token",
    "atlas-gemini-api-key",
    "atlas-cerebras-api-key",
    "atlas-app-encryption-key",
    "atlas-trongrid-api-key",
    "subprocess.check_output",
    "mdawmdawmdawmdawmdawmdawmdawmdawmdawmdawmda=",
}

def _inside_quoted_literal(line: str, position: int) -> bool:
    """Return whether position is inside a single-, double-, or backtick string."""
    quote = None
    escaped = False
    for char in line[:position]:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif quote is not None:
            if char == quote:
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
    return quote is not None


CODE_ATTRIBUTE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
ANGLE_PLACEHOLDER = re.compile(
    r"(?i)^<(?:your(?:[-_ ][a-z0-9_-]+)?|placeholder|example|redacted|unset|"
    r"not[-_ ]set|secret|private[-_ ]key|api[-_ ]key|token|value|version|ref|v)>$"
)


def _is_helper_call(value: str, remainder: str) -> bool:
    """Recognize a call expression without exempting a literal with a parenthesized note."""
    if not (HELPER_IDENTIFIER.fullmatch(value) or CODE_ATTRIBUTE.fullmatch(value)):
        return False
    if not remainder.startswith("("):
        return False
    arguments = remainder[1:].lstrip()
    if not arguments or arguments.startswith(")"):
        return True
    if arguments[0] in {"'", '"', "[", "{"}:
        return True
    identifier = re.match(r"[A-Za-z_][A-Za-z0-9_]*", arguments)
    if identifier:
        suffix = arguments[identifier.end():].lstrip()
        return suffix.startswith((",", ")", ".", "[", "="))
    return bool(re.match(r"\d+\s*[,\)\]]", arguments))


def scan() -> list[str]:
    findings=[]
    for path in ROOT.rglob("*"):
        if not path.is_file() or (path.suffix.lower() not in TEXT_SUFFIXES and not path.name.lower().startswith(".env")):
            continue  # ".env" has an empty suffix, so match dotenv files by name too
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try: text=path.read_text(errors="ignore")
        except OSError: continue
        for lineno,line in enumerate(text.splitlines(),1):
            if PRIVATE_KEY.search(line):
                findings.append(f"{path.relative_to(ROOT)}:{lineno}: private-key material")
                continue
            seed_match = TRON_SEED.search(line)
            if seed_match and "re.compile" not in line:
                seed_value = seed_match.group("value").strip().strip('"').strip("'").strip().lower()
                if seed_value not in PLACEHOLDERS and not ANGLE_PLACEHOLDER.fullmatch(seed_value):
                    findings.append(f"{path.relative_to(ROOT)}:{lineno}: seed/private-key assignment")
                    continue
            for match in ASSIGNMENT.finditer(line):
                value = match.group("value").lower()
                remainder = line[match.end():].lstrip()
                source_code = path.suffix.lower() in SOURCE_CODE_SUFFIXES
                quoted_literal = source_code and _inside_quoted_literal(
                    line, match.start("value")
                )
                # In source code, a bare attribute reference or helper call is
                # configuration wiring, not a credential literal. Keep scanning
                # quoted strings (including embedded password= fragments) and
                # all YAML/dotenv/config assignments.
                if source_code and not quoted_literal and CODE_ATTRIBUTE.fullmatch(value):
                    continue
                # Helper calls can also appear in embedded workflow scripts or
                # source snippets inside test strings. A long literal followed
                # by a note such as (rotation-note) is not a valid call suffix.
                if _is_helper_call(value, remainder):
                    continue
                # Fixture exemptions are exact values, never substring markers.
                if value in PLACEHOLDERS:
                    continue
                findings.append(f"{path.relative_to(ROOT)}:{lineno}: credential-like assignment")
    return findings

if __name__ == "__main__":
    findings=scan()
    if findings:
        print("\n".join(findings))
        sys.exit(1)
    print("No high-signal repository secrets detected.")
