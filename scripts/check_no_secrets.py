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
ASSIGNMENT = re.compile(r"""(?i)^\s*(?:export\s+)?(?:<[^>]+?\s+)?[{,\[]?\s*["']?(?:[A-Za-z_][A-Za-z0-9_]*_)?(?:api[_-]?key|api[_-]?secret|secret[_-]?key|private[_-]?key|encryption[_-]?key|signing[_-]?key|service[_-]?role[_-]?key|password|token|mnemonic|seed[_-]?phrase)["']?\s*[:=]\s*['"]?([A-Za-z0-9_+/=.-]{20,})""")
PRIVATE_KEY = re.compile(r"^\s*-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----\s*$")
TRON_SEED = re.compile(r"""(?i)^\s*(?:export\s+)?["']?(?:mnemonic|seed[_-]?phrase|private[_-]?key)["']?\s*[:=]\s*([^,\s'"]+)""")

PLACEHOLDERS = {
    "changeme", "change-me", "example", "placeholder", "your-secret", "your-key",
    "test-password", "verification-password", "verification", "atlas-ci", "fake-key",
    "generate_a_fernet_key_and_store_in_secret_manager", "fixture", "ci-only", "test_",
    "must-not-", "legacy-", "supported-", "distinct-", "different-",
    # Fixed non-production Fernet test key declared in production-verification.yml.
    "mda0mda0mda0mda0mda0mda0mda0mda0mda0mda0mda=",
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
            m = ASSIGNMENT.search(line)
            if not m:
                continue
            value = m.group(1).lower()
            remainder = line[m.end():].lstrip()
            if remainder.startswith("("):  # derived token/helper call, not a literal credential
                continue
            if value in PLACEHOLDERS or value.startswith("<") or "." in value or "(" in value:
                continue
            if any(safe in value for safe in ("verification", "test-", "fake-", "example", "placeholder", "changeme", "fixture", "ci-only", "must-not-", "legacy-", "supported-", "distinct-", "different-")):
                continue
            if "your-" not in value:
                findings.append(f"{path.relative_to(ROOT)}:{lineno}: credential-like assignment")
    return findings

if __name__ == "__main__":
    findings=scan()
    if findings:
        print("\n".join(findings))
        sys.exit(1)
    print("No high-signal repository secrets detected.")
