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
ASSIGNMENT = re.compile(r"""(?i)(?<![A-Za-z0-9_])["']?(?P<key>(?:[A-Za-z_][A-Za-z0-9]*_)?(?:api[_-]?key|api[_-]?secret|secret[_-]?key|private[_-]?key|encryption[_-]?key|signing[_-]?key|service[_-]?role[_-]?key|password|token|mnemonic|seed[_-]?phrase))["']?\s*[:=]\s*["']?(?P<value>[A-Za-z0-9_+/=.-]{20,})""")
SOURCE_CODE_SUFFIXES = {".py", ".js", ".java", ".kt", ".sh", ".bash"}
HELPER_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PRIVATE_KEY = re.compile(r"^\s*-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----\s*$")
TRON_SEED = re.compile(r"""(?i)^\s*(?:export\s+)?["']?(?:mnemonic|seed[_-]?phrase|private[_-]?key)["']?\s*[:=]\s*([^,\s'"]+)""")

PLACEHOLDERS = {
    "changeme", "change-me", "example", "placeholder", "your-secret", "your-key",
    "test-password", "verification-password", "verification", "atlas-ci", "fake-key",
    "generate_a_fernet_key_and_store_in_secret_manager", "fixture", "ci-only",
    "paper_test_password", "test_password", "distinct-paper-secret",
    "different-primary-secret", "legacy-secret", "supported-review-key",
    "legacy-hosted-provider", "must-not-become-a-helper-setting", "must-not-be-persisted",
    # Exact values used only by repository verification fixtures.
    "ci-only-keystore-password",
    "mdawmdawmdawmdawmdawmdawmdawmdawmdawmdawmda=",
}

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
            if seed_match and "re.compile" not in line and not any(p in seed_match.group(1).lower() for p in PLACEHOLDERS) and "<" not in seed_match.group(1):
                findings.append(f"{path.relative_to(ROOT)}:{lineno}: seed/private-key assignment")
                continue
            for match in ASSIGNMENT.finditer(line):
                value = match.group("value").lower()
                remainder = line[match.end():].lstrip()
                # Only a bare function call in source code is a derived value.
                # Config scalars such as TOKEN: long-value(rotation-note) remain
                # credentials and must not be exempted by the same syntax.
                if (
                    path.suffix.lower() in SOURCE_CODE_SUFFIXES
                    and HELPER_IDENTIFIER.fullmatch(value)
                    and remainder.startswith("(")
                ):
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
