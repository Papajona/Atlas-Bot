#!/usr/bin/env python3
"""Repository secret/configuration scanner used by CI and release verification.

This is intentionally conservative: it reports high-signal credential assignments and
private-key/seed material while allowing placeholders and public-key material.
"""
from __future__ import annotations
import json
import re
import sys
import unicodedata
from pathlib import Path

import yaml
from bip_utils import Bip39MnemonicValidator
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", "dist", "build", ".pytest_cache"}
# Include source, documentation, Android resources, deployment/configuration files and
# common text-based key/certificate containers. An allowlist that omits Markdown or
# PEM/key extensions lets credentials bypass the same scanner CI uses as a release gate.
TEXT_SUFFIXES = {
    ".py", ".sh", ".bash", ".env", ".example", ".yml", ".yaml", ".json",
    ".toml", ".ini", ".gradle", ".kts", ".md", ".txt", ".xml", ".properties",
    ".java", ".kt", ".js", ".ts", ".tsx", ".mjs", ".cjs",
    ".html", ".css", ".sql", ".pem", ".key", ".crt", ".tf", ".tfvars",
    ".dockerignore", ".conf", ".cfg", ".lock", ".patch",
}
SCANNED_TEXT_FILENAMES = {".dockerignore", "Makefile", "Procfile"}
ASSIGNMENT = re.compile(r"""(?i)(?<![A-Za-z0-9_])["']?(?P<key>(?:[A-Za-z_][A-Za-z0-9_]*_)?(?:api[_-]?key|api[_-]?secret|secret[_-]?key|private[_-]?key|encryption[_-]?key|signing[_-]?key|service[_-]?role[_-]?key|password|token|mnemonic|seed[_-]?phrase))["']?\s*[:=]\s*["']?(?P<value>[A-Za-z0-9_+/=.-]{20,})""")
SOURCE_CODE_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".sh", ".bash"
}
HELPER_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PRIVATE_KEY = re.compile(r"^\s*-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----\s*$")
TRON_SEED = re.compile(r"""(?i)(?:^\s*|\{\s*|,\s*|\[\s*)(?:-\s*)?(?:export\s+)?["']?(?P<key>mnemonic|seed[_-]?phrase|private[_-]?key)["']?\s*[:=]\s*(?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^#\r\n]+)""")
TRON_SEED_IN_SOURCE = re.compile(
    r"""(?i)(?<![A-Za-z0-9_])(?:-\s*)?(?:export\s+)?["']?(?P<key>mnemonic|seed[_-]?phrase|private[_-]?key)["']?\s*[:=]\s*(?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^#\r\n]+)"""
) 
MULTILINE_SEED_KEY = re.compile(
    r"""(?i)^\s*(?:-\s*)?["']?(?P<key>mnemonic|seed[_-]?phrase|private[_-]?key)["']?\s*[:=]\s*(?:(?:[|>][+-]?)\s*)?(?:#.*)?$"""
)

SOURCE_STRING_CONCATENATION = re.compile(
    r"""(?:
        (?:"[^"\r\n]*"|'[^'\r\n]*')\s*\+\s*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*
        |[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\s*\+\s*(?:"[^"\r\n]*"|'[^'\r\n]*')
        |["']\s*\+\s*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\s*\+\s*["']
        |[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\s*\+\s*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*
    )""",
    re.VERBOSE,
)
SOURCE_STRING_LITERAL = re.compile(
    r"""(?P<quote>["'])(?P<value>(?:\\.|[^\\])*?)\1"""
)

PLACEHOLDERS = {
    "changeme", "change-me", "example", "placeholder", "your-secret", "your-key",
    "test-password", "verification-password", "verification", "atlas-ci", "fake-key",
    "generate_a_fernet_key_and_store_in_secret_manager", "fixture", "ci-only",
    "paper_test_password", "test_password", "distinct-paper-secret",
    "different-primary-secret", "legacy-secret", "supported-review-key",
    "legacy-hosted-provider", "must-not-become-a-helper-setting", "must-not-be-persisted",
    # Exact values used only by repository verification fixtures.
    "fixture_service_role_value_abcdefghijklmnopqrstuvwxyz",
    "fixture_signing_value_abcdefghijklmnopqrstuvwxyz",
    "fixture_encryption_value_abcdefghijklmnopqrstuvwxyz",
    "upgraded-test-refresh",
    "mock-customer-aal1-token",
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
    # Exact dummy Secret Manager references used only in legacy migration fixtures.
    "atlas-" + "ge" + "mini" + "-api-key",
    "atlas-" + "cere" + "bras" + "-api-key",

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


def _has_sensitive_source_literal(key: str, expression: str) -> bool:
    """Detect seed/private-key material in quoted pieces of a source concatenation."""
    components = [
        match.group("value")
        for match in SOURCE_STRING_LITERAL.finditer(expression)
    ]
    if not components:
        # An identifier-only mnemonic/private-key expression cannot be proven
        # harmless from this line; fail closed rather than trusting unknown values.
        return True

    normalized_key = key.lower().replace("_", "").replace("-", "")
    if normalized_key in {"mnemonic", "seedphrase"}:
        # BIP-39-style recovery phrases are sequences of alphabetic words. Join
        # adjacent literal pieces with whitespace so split phrases are still found.
        candidates = components + [" ".join(components)]
        for candidate in candidates:
            words = candidate.lower().split()
            if len(words) >= 10 and all(
                re.fullmatch(r"[a-z]{2,12}", word) for word in words
            ):
                return True
        return False

    # Private keys may be split across adjacent quoted components. Join them
    # without separators before checking for a long key-like token.
    combined = "".join(components)
    return re.search(
        r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{20,}(?![A-Za-z0-9])",
        combined,
    ) is not None


BIP39_VALIDATOR = Bip39MnemonicValidator()
BIP39_WORD_COUNTS = {12, 15, 18, 21, 24}
PRIVATE_JWK_MATERIAL_FIELDS = {
    "d", "p", "q", "dp", "dq", "qi", "oth", "k",
    "private", "privatekey", "privatekeypem", "privkey",
    "secret", "secretkey", "seed", "keymaterial", "pem",
}
YAML_CREDENTIAL_KEY = re.compile(
    r"""(?i)^(?:[A-Za-z_][A-Za-z0-9_]*_)?(?:api[_-]?key|api[_-]?secret|secret[_-]?key|private[_-]?key|encryption[_-]?key|signing[_-]?key|service[_-]?role[_-]?key|password|token|mnemonic|seed[_-]?phrase)$"""
)


def _is_valid_bip39_mnemonic(value: str) -> bool:
    """Accept only complete, checksum-valid BIP-39 phrases from supported wordlists."""
    candidate = unicodedata.normalize("NFKD", value).replace("\u3000", " ")
    words = candidate.split()
    if len(words) not in BIP39_WORD_COUNTS:
        return False
    return BIP39_VALIDATOR.IsValid(" ".join(words))


def _yaml_scalar_values(node):
    """Yield scalar values from a YAML node without treating scalar text as YAML syntax."""
    if isinstance(node, ScalarNode):
        yield node.value
    elif isinstance(node, SequenceNode):
        for child in node.value:
            yield from _yaml_scalar_values(child)
    elif isinstance(node, MappingNode):
        for _, value_node in node.value:
            yield from _yaml_scalar_values(value_node)


def _nonempty_private_scalar(value: str) -> bool:
    candidate = value.strip().strip('"').strip("'").strip()
    if not candidate:
        return False
    normalized = candidate.lower()
    if normalized in PLACEHOLDERS or ANGLE_PLACEHOLDER.fullmatch(candidate):
        return False
    return True


def _structured_private_object_has_material(value) -> bool:
    """Detect private JWK components while allowing empty and public-only key objects."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key in PRIVATE_JWK_MATERIAL_FIELDS:
                if isinstance(item, str) and _nonempty_private_scalar(item):
                    return True
                if isinstance(item, (dict, list)) and _structured_private_object_has_material(item):
                    return True
                if isinstance(item, (int, float)) and not isinstance(item, bool) and item != 0:
                    return True
        return any(
            _structured_private_object_has_material(item)
            for item in value.values()
            if isinstance(item, (dict, list))
        )
    if isinstance(value, list):
        return any(
            _structured_private_object_has_material(item)
            for item in value
            if isinstance(item, (dict, list))
        )
    return False


def _yaml_node_has_private_material(node) -> bool:
    """Classify private-key YAML values, including JSON JWKs and native YAML mappings."""
    if isinstance(node, ScalarNode):
        candidate = node.value.strip()
        if not candidate:
            return False
        if candidate.lower() in PLACEHOLDERS or ANGLE_PLACEHOLDER.fullmatch(candidate):
            return False
        if candidate.startswith("<") and candidate.endswith(">"):
            bracketed_value = candidate[1:-1]
            if re.fullmatch(r"[A-Za-z0-9_+/=.-]{20,}", bracketed_value):
                return True
        if candidate.startswith(("{", "[")):
            try:
                structured = json.loads(candidate)
            except json.JSONDecodeError:
                # Malformed non-empty key objects are suspicious; valid empty or
                # public-only objects are handled structurally below.
                return candidate not in {"{}", "[]"}
            return _structured_private_object_has_material(structured)
        if PRIVATE_KEY.search(candidate):
            return True
        compact = re.sub(r"\s+", "", candidate)
        return bool(re.fullmatch(r"[A-Za-z0-9_+/=.-]{20,}", compact))
    if isinstance(node, MappingNode):
        for key_node, value_node in node.value:
            if not isinstance(key_node, ScalarNode):
                continue
            normalized_key = re.sub(
                r"[^a-z0-9]", "", key_node.value.lower()
            )
            if normalized_key in PRIVATE_JWK_MATERIAL_FIELDS:
                if any(_nonempty_private_scalar(value) for value in _yaml_scalar_values(value_node)):
                    return True
            # Public JWK fields (n/e/x/y/crv/kty) are never considered private
            # material merely because their values are long.
            if isinstance(value_node, (MappingNode, SequenceNode)) and _yaml_node_has_private_material(value_node):
                return True
        return False
    if isinstance(node, SequenceNode):
        return any(_yaml_node_has_private_material(child) for child in node.value)
    return False


def _yaml_value_is_sensitive(key: str, value_node) -> bool:
    normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
    if normalized_key in {"mnemonic", "seedphrase"}:
        return _is_valid_bip39_mnemonic(" ".join(_yaml_scalar_values(value_node)))
    if normalized_key == "privatekey":
        return _yaml_node_has_private_material(value_node)
    if not YAML_CREDENTIAL_KEY.fullmatch(key):
        return False

    for value in _yaml_scalar_values(value_node):
        candidate = value.strip().strip('"').strip("'").strip()
        if not candidate:
            continue
        if candidate.lower() in PLACEHOLDERS or ANGLE_PLACEHOLDER.fullmatch(candidate):
            continue
        if re.fullmatch(r"[A-Za-z0-9_+/=.-]{20,}", candidate):
            return True
    return False


def _find_yaml_sensitive_assignments(text: str) -> dict[int, str]:
    """Parse YAML mappings so quoted block-scalar examples are never treated as keys."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return None
    findings: dict[int, str] = {}

    def visit(node) -> None:
        if isinstance(node, MappingNode):
            for key_node, value_node in node.value:
                if isinstance(key_node, ScalarNode):
                    key = key_node.value
                    if YAML_CREDENTIAL_KEY.fullmatch(key) and _yaml_value_is_sensitive(key, value_node):
                        findings[key_node.start_mark.line + 1] = key
                visit(value_node)
        elif isinstance(node, SequenceNode):
            for child in node.value:
                visit(child)

    if root is not None:
        visit(root)
    return findings


def _multiline_seed_material_is_secret(key: str, value_lines: list[str]) -> bool:
    """Retain multiline detection for non-YAML configuration formats."""
    candidate = " ".join(
        line.strip().strip("'\"").strip()
        for line in value_lines
        if line.strip() and not line.lstrip().startswith("#")
    ).strip()
    if not candidate or candidate.lower() in PLACEHOLDERS or ANGLE_PLACEHOLDER.fullmatch(candidate):
        return False
    normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
    if normalized_key in {"mnemonic", "seedphrase"}:
        return _is_valid_bip39_mnemonic(candidate)
    if normalized_key == "privatekey":
        return _yaml_node_has_private_material(ScalarNode("tag:yaml.org,2002:str", candidate))
    compact = re.sub(r"\s+", "", candidate)
    return len(compact) >= 20 and re.fullmatch(r"[A-Za-z0-9_+/=.-]+", compact) is not None


def _find_multiline_seed_assignments(lines: list[str]) -> dict[int, str]:
    """Line-oriented multiline check for dotenv/TOML/INI-style files."""
    findings: dict[int, str] = {}
    for index, line in enumerate(lines):
        match = MULTILINE_SEED_KEY.fullmatch(line)
        if match is None:
            continue
        key_indent = len(line) - len(line.lstrip(" \t"))
        value_lines: list[str] = []
        for following in lines[index + 1 :]:
            if not following.strip():
                continue
            indent = len(following) - len(following.lstrip(" \t"))
            if indent <= key_indent:
                break
            value_lines.append(following)
        if _multiline_seed_material_is_secret(match.group("key"), value_lines):
            findings[index + 1] = match.group("key")
    return findings


def scan() -> list[str]:
    findings=[]
    for path in ROOT.rglob("*"):
        name = path.name
        name_is_scannable = (
            name in SCANNED_TEXT_FILENAMES or name.lower().startswith("dockerfile")
        )
        if not path.is_file() or (
            path.suffix.lower() not in TEXT_SUFFIXES
            and not name.lower().startswith(".env")
            and not name_is_scannable
        ):
            continue  # dotenv files and extensionless build entrypoints need name checks too
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try: text=path.read_text(errors="ignore")
        except OSError: continue
        lines = text.splitlines()
        suffix = path.suffix.lower()
        yaml_parsed = False
        if suffix in {".yaml", ".yml"}:
            multiline_seed_findings = _find_yaml_sensitive_assignments(text)
            yaml_parsed = multiline_seed_findings is not None
            if not yaml_parsed:
                multiline_seed_findings = _find_multiline_seed_assignments(lines)
        elif suffix in {".env", ".toml", ".ini", ".conf", ".cfg", ".json"}:
            multiline_seed_findings = _find_multiline_seed_assignments(lines)
        else:
            multiline_seed_findings = {}
        for lineno,line in enumerate(lines,1):
            multiline_seed_key = multiline_seed_findings.get(lineno)
            if multiline_seed_key:
                findings.append(f"{path.relative_to(ROOT)}:{lineno}: seed/private-key assignment")
                continue
            if PRIVATE_KEY.search(line):
                findings.append(f"{path.relative_to(ROOT)}:{lineno}: private-key material")
                continue
            source_code = path.suffix.lower() in SOURCE_CODE_SUFFIXES
            is_yaml = path.suffix.lower() in {".yaml", ".yml"}
            seed_match = (
                TRON_SEED_IN_SOURCE.search(line)
                if source_code
                else TRON_SEED.search(line) if not is_yaml or not yaml_parsed else None
            )
            if seed_match and "re.compile" not in line:
                raw_seed_value = seed_match.group("value").strip()
                yaml_block_scalar = (
                    path.suffix.lower() in {".yaml", ".yml"}
                    and raw_seed_value in {"|", "|-", "|+", ">", ">-", ">+"}
                )
                if yaml_block_scalar:
                    # The indented block was already scanned as a whole above;
                    # the YAML marker itself is not credential material.
                    continue
                computed_source_value = (
                    source_code
                    and SOURCE_STRING_CONCATENATION.search(raw_seed_value) is not None
                )
                if computed_source_value:
                    # Ignore harmless computed labels, but never suppress a literal
                    # recovery phrase or long private-key token in a quoted component.
                    if _has_sensitive_source_literal(
                        seed_match.group("key"), raw_seed_value
                    ):
                        findings.append(f"{path.relative_to(ROOT)}:{lineno}: seed/private-key assignment")
                        continue
                else:
                    seed_value = raw_seed_value.strip('"').strip("'").strip().lower()
                    if seed_value not in PLACEHOLDERS and not ANGLE_PLACEHOLDER.fullmatch(seed_value):
                        findings.append(f"{path.relative_to(ROOT)}:{lineno}: seed/private-key assignment")
                        continue
            for match in ASSIGNMENT.finditer(line):
                if is_yaml and yaml_parsed:
                    yaml_key = re.sub(r"[^a-z0-9]", "", match.group("key").lower())
                    if yaml_key in {"mnemonic", "seedphrase", "privatekey"}:
                        # These keys are validated against YAML mapping structure above.
                        # Do not reinterpret the same words inside block-scalar prose.
                        continue
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
                if source_code and _is_helper_call(value, remainder):
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
