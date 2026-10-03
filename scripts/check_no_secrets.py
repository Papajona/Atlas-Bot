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
TEXT_SUFFIXES = {".py",".sh",".bash",".env",".example",".yml",".yaml",".json",".toml",".ini",".gradle",".kts"}
ASSIGNMENT = re.compile(r"(?i)\b(?:api[_-]?key|api[_-]?secret|secret[_-]?key|password|token|mnemonic|seed[_-]?phrase)\s*[:=]\s*['\"]?([A-Za-z0-9_+/=.-]{20,})")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----")
TRON_SEED = re.compile(r"(?i)\b(?:mnemonic|seed[_-]?phrase|private[_-]?key)\s*[:=]")

PLACEHOLDERS = {"changeme","change-me","example","placeholder","your-secret","your-key","test-password","verification-password","verification","atlas-ci","fake-key","generate_a_fernet_key_and_store_in_secret_manager"}

def scan() -> list[str]:
    findings=[]
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try: text=path.read_text(errors="ignore")
        except OSError: continue
        for lineno,line in enumerate(text.splitlines(),1):
            if PRIVATE_KEY.search(line):
                findings.append(f"{path.relative_to(ROOT)}:{lineno}: private-key material")
                continue
            if TRON_SEED.search(line) and not any(p in line.lower() for p in PLACEHOLDERS):
                findings.append(f"{path.relative_to(ROOT)}:{lineno}: seed/private-key assignment")
                continue
            m=ASSIGNMENT.search(line)
            if m and m.group(1).lower() not in PLACEHOLDERS and not m.group(1).startswith("<"):
                if any(safe in m.group(1).lower() for safe in ("verification", "test-", "fake-", "example", "placeholder", "changeme")):
                    continue
                if "your-" not in m.group(1).lower():
                    findings.append(f"{path.relative_to(ROOT)}:{lineno}: credential-like assignment")
    return findings

if __name__ == "__main__":
    findings=scan()
    if findings:
        print("\n".join(findings))
        sys.exit(1)
    print("No high-signal repository secrets detected.")
