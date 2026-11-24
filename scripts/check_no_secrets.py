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
from bip_utils.utils.mnemonic.mnemonic_ex import MnemonicChecksumError
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
ASSIGNMENT = re.compile(r"""(?i)(?<![A-Za-z0-9_])["']?(?P<key>(?:[A-Za-z_][A-Za-z0-9_]*\.)*(?:[A-Za-z_][A-Za-z0-9_]*_)?(?:api[_-]?key|api[_-]?secret|secret[_-]?key|private[_-]?key|encryption[_-]?key|signing[_-]?key|service[_-]?role[_-]?key|password|token|mnemonic|seed|seed[_-]?phrase|recovery[_-]?phrase))["']?\s*[:=]\s*["']?(?P<value>[A-Za-z0-9_+/=.-]{20,})""")
SOURCE_CODE_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".sh", ".bash"
}
HELPER_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PRIVATE_KEY = re.compile(r"^\s*-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----\s*$")
TRON_SEED = re.compile(r"""(?i)(?:^\s*|\{\s*|,\s*|\[\s*)(?:[A-Za-z_][A-Za-z0-9_]*\.)*(?:-\s*)?(?:export\s+)?["']?(?P<key>mnemonic|seed|seed[_-]?phrase|recovery[_-]?phrase|private[_-]?key)["']?\s*[:=]\s*(?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^#\r\n]+)""")
TRON_SEED_IN_SOURCE = re.compile(
    r"""(?i)(?<![A-Za-z0-9_])(?:[A-Za-z_][A-Za-z0-9_]*\.)*(?:-\s*)?(?:export\s+)?["']?(?P<key>mnemonic|seed|seed[_-]?phrase|recovery[_-]?phrase|private[_-]?key)["']?\s*[:=]\s*(?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^#\r\n]+)"""
)
MULTILINE_SEED_KEY = re.compile(
    r"""(?i)^\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)*(?:-\s*)?["']?(?P<key>mnemonic|seed|seed[_-]?phrase|recovery[_-]?phrase|private[_-]?key)["']?\s*[:=]\s*(?:(?:[|>][+-]?)\s*)?(?:#.*)?$"""
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

    normalized_key = key.rsplit(".", 1)[-1].lower().replace("_", "").replace("-", "")
    if normalized_key in {"mnemonic", "seed", "seedphrase", "recoveryphrase"}:
        candidates = components + [" ".join(components)]
        return any(_is_likely_recovery_phrase(candidate) for candidate in candidates)

    # Private keys may be split across adjacent quoted components. Join them
    # without separators before checking for a long key-like token.
    combined = "".join(components)
    return re.search(
        r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{20,}(?![A-Za-z0-9])",
        combined,
    ) is not None


BIP39_VALIDATOR = Bip39MnemonicValidator()
BIP39_WORD_COUNTS = {12, 15, 18, 21, 24}
MAX_YAML_NODE_VISITS = 4096
MAX_YAML_DOCUMENT_NODES = 65536
MAX_YAML_TRAVERSAL_DEPTH = 64


class _YamlTraversalLimitExceeded(ValueError):
    """Raised when alias expansion or nesting exceeds the bounded scanner budget."""


def _charge_yaml_traversal(budget: list[int], depth: int) -> None:
    if depth > MAX_YAML_TRAVERSAL_DEPTH or budget[0] >= MAX_YAML_NODE_VISITS:
        raise _YamlTraversalLimitExceeded("YAML alias expansion exceeded the scan budget")
    budget[0] += 1
PRIVATE_JWK_MATERIAL_FIELDS = {
    "d", "p", "q", "dp", "dq", "qi", "oth", "k",
    "private", "privatekey", "privatekeypem", "privkey",
    "secret", "secretkey", "seed", "key", "value", "keymaterial", "pem",
}
YAML_CREDENTIAL_KEY = re.compile(
    r"""(?i)^(?:[A-Za-z_][A-Za-z0-9_]*\.)*(?:[A-Za-z_][A-Za-z0-9_]*_)?(?:api[_-]?key|api[_-]?secret|secret[_-]?key|private[_-]?key|encryption[_-]?key|signing[_-]?key|service[_-]?role[_-]?key|password|token|mnemonic|seed|seed[_-]?phrase|recovery[_-]?phrase)$"""
)


RECOVERY_INSTRUCTION_WORDS = {
    "write", "down", "store", "safely", "safe", "somewhere", "keep", "your",
    "these", "words", "word", "should", "must", "please", "never", "share",
    "copy", "record", "backup", "offline", "secure", "enter", "type",
    "instructions", "instruction", "recovery", "phrase",
}

def _is_bip39_wordlist_phrase(value: str) -> bool:
    """Detect full-length phrases from supported BIP-39 wordlists, even with a bad checksum."""
    candidate = unicodedata.normalize("NFKD", value).replace("\u3000", " ")
    words = candidate.split()
    if len(words) not in BIP39_WORD_COUNTS:
        return False
    try:
        BIP39_VALIDATOR.Validate(" ".join(words))
    except MnemonicChecksumError:
        # All words belong to a supported list and the phrase length is valid;
        # a bad checksum can still be a mistyped checked-in recovery phrase.
        return True
    except ValueError:
        return False
    return True


def _is_likely_recovery_phrase(value: str) -> bool:
    """Detect BIP-39 and non-BIP-39 word sequences stored under a recovery-key name."""
    candidate = unicodedata.normalize("NFKD", value).replace("\u3000", " ")
    if _is_bip39_wordlist_phrase(candidate):
        return True

    # Some wallets and providers use proprietary word lists rather than BIP-39.
    # The key name is already an explicit recovery-secret signal. Catch
    # proprietary recovery phrases shorter than BIP-39's standard lengths too,
    # while avoiding ordinary prose and unrelated non-Latin operational notes.
    words = candidate.split()
    if len(words) == 1:
        token = words[0]
        return (
            token.lower() not in PLACEHOLDERS
            and not ANGLE_PLACEHOLDER.fullmatch(token)
            and re.fullmatch(r"[A-Za-z0-9_+/=-]{20,}", token) is not None
        )
    normalized_words = [word.lower().strip(".,;:!?()[]{}") for word in words]
    if any(word in RECOVERY_INSTRUCTION_WORDS for word in normalized_words):
        return False
    return len(words) >= 6 and all(
        re.fullmatch(r"[A-Za-z][A-Za-z0-9'-]{1,23}", word) is not None
        for word in words
    )


def _yaml_scalar_values(
    node,
    active_nodes: set[int] | None = None,
    budget: list[int] | None = None,
    depth: int = 0,
):
    """Yield YAML scalars while preserving aliases and bounding graph expansion."""
    if node is None:
        return
    if active_nodes is None:
        active_nodes = set()
    if budget is None:
        budget = [0]
    node_identity = id(node)
    if node_identity in active_nodes:
        return
    _charge_yaml_traversal(budget, depth)
    active_nodes.add(node_identity)
    try:
        if isinstance(node, ScalarNode):
            yield node.value
        elif isinstance(node, SequenceNode):
            for child in node.value:
                yield from _yaml_scalar_values(
                    child, active_nodes, budget, depth + 1
                )
        elif isinstance(node, MappingNode):
            for _, value_node in node.value:
                yield from _yaml_scalar_values(
                    value_node, active_nodes, budget, depth + 1
                )
    finally:
        active_nodes.remove(node_identity)


def _nonempty_private_scalar(value: str) -> bool:
    candidate = value.strip().strip('"').strip("'").strip()
    if not candidate:
        return False
    normalized = candidate.lower()
    if normalized in {"null", "none", "~", "true", "false"}:
        return False
    if normalized in PLACEHOLDERS or ANGLE_PLACEHOLDER.fullmatch(candidate):
        return False
    return True


def _structured_private_object_has_material(value) -> bool:
    """Detect private JWK components while keeping public metadata arrays harmless."""
    stack = [(value, isinstance(value, list))]
    visited: set[tuple[int, bool]] = set()
    visits = 0
    while stack:
        current, scalar_items_are_private = stack.pop()
        if not isinstance(current, (dict, list)):
            continue
        identity = (id(current), scalar_items_are_private)
        if identity in visited:
            continue
        visited.add(identity)
        visits += 1
        if visits > MAX_YAML_NODE_VISITS:
            raise _YamlTraversalLimitExceeded(
                "structured private-key object exceeded the scan budget"
            )
        if isinstance(current, dict):
            for key, item in current.items():
                normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
                is_private_component = normalized_key in PRIVATE_JWK_MATERIAL_FIELDS
                if is_private_component:
                    if isinstance(item, str) and _nonempty_private_scalar(item):
                        return True
                    if isinstance(item, (int, float)) and not isinstance(item, bool) and item != 0:
                        return True
                if isinstance(item, (dict, list)):
                    stack.append((item, is_private_component))
        else:
            for item in current:
                if isinstance(item, dict):
                    stack.append((item, False))
                elif isinstance(item, list):
                    stack.append((item, scalar_items_are_private))
                elif scalar_items_are_private:
                    if isinstance(item, str) and _nonempty_private_scalar(item):
                        return True
                    if isinstance(item, (int, float)) and not isinstance(item, bool) and item != 0:
                        return True
    return False

def _yaml_node_has_private_material(
    node,
    active_nodes: set[int] | None = None,
    budget: list[int] | None = None,
    depth: int = 0,
) -> bool:
    """Classify private-key YAML values without loops or unbounded alias expansion."""
    if node is None:
        return False
    if active_nodes is None:
        active_nodes = set()
    if budget is None:
        budget = [0]
    node_identity = id(node)
    if node_identity in active_nodes:
        return False
    _charge_yaml_traversal(budget, depth)
    active_nodes.add(node_identity)
    try:
        if isinstance(node, ScalarNode):
            candidate = node.value.strip()
            if not candidate or candidate.lower() in {"null", "none", "~", "true", "false"}:
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
                except RecursionError:
                    # Deeply nested JSON key material cannot be inspected safely.
                    return True
                except json.JSONDecodeError:
                    # Malformed non-empty key objects are suspicious; valid empty or
                    # public-only objects are handled structurally below.
                    return candidate not in {"{}", "[]"}
                return _structured_private_object_has_material(structured)
            if PRIVATE_KEY.search(candidate):
                return True
            return bool(
                re.search(r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=.-]{20,}(?![A-Za-z0-9])", candidate)
            )
        if isinstance(node, MappingNode):
            for key_node, value_node in node.value:
                if not isinstance(key_node, ScalarNode):
                    continue
                normalized_key = re.sub(
                    r"[^a-z0-9]", "", key_node.value.lower()
                )
                if normalized_key in PRIVATE_JWK_MATERIAL_FIELDS:
                    if any(
                        _nonempty_private_scalar(value)
                        for value in _yaml_scalar_values(
                            value_node, budget=budget, depth=depth + 1
                        )
                    ):
                        return True
                # Public JWK fields (n/e/x/y/crv/kty) are not private merely
                # because their values are long. Unknown nested mappings still
                # need inspection for nested private components such as key.
                if isinstance(value_node, (MappingNode, SequenceNode)) and _yaml_node_has_private_material(
                    value_node, active_nodes, budget, depth + 1
                ):
                    return True
            return False
        if isinstance(node, SequenceNode):
            return any(
                _yaml_node_has_private_material(
                    child, active_nodes, budget, depth + 1
                )
                for child in node.value
            )
        return False
    finally:
        active_nodes.remove(node_identity)


def _yaml_value_is_sensitive(key: str, value_node) -> bool:
    normalized_key = re.sub(r"[^a-z0-9]", "", key.rsplit(".", 1)[-1].lower())
    if normalized_key in {"mnemonic", "seed", "seedphrase", "recoveryphrase"}:
        try:
            return _is_likely_recovery_phrase(
                " ".join(_yaml_scalar_values(value_node))
            )
        except _YamlTraversalLimitExceeded:
            # A sensitive field that cannot be fully inspected is suspicious.
            return True
    if normalized_key == "privatekey":
        try:
            return _yaml_node_has_private_material(value_node)
        except _YamlTraversalLimitExceeded:
            return True
    if not YAML_CREDENTIAL_KEY.fullmatch(key):
        return False

    try:
        for value in _yaml_scalar_values(value_node):
            candidate = value.strip().strip('"').strip("'").strip()
            if not candidate:
                continue
            if candidate.lower() in PLACEHOLDERS or ANGLE_PLACEHOLDER.fullmatch(candidate):
                continue
            if re.fullmatch(r"[A-Za-z0-9_+/=.-]{20,}", candidate):
                return True
    except _YamlTraversalLimitExceeded:
        return True
    return False


def _find_yaml_sensitive_assignments(text: str) -> dict[int, str]:
    """Parse YAML mappings with iterative, bounded traversal of alias graphs."""
    try:
        roots = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    except RecursionError:
        # The YAML parser itself hit the recursion limit; fail closed.
        return {1: "YAML parsing recursion limit exceeded"}
    except yaml.YAMLError:
        return None
    findings: dict[int, str] = {}
    visited_nodes: set[int] = set()
    stack = [root for root in reversed(roots) if root is not None]
    document_visits = 0

    while stack:
        node = stack.pop()
        node_identity = id(node)
        if node_identity in visited_nodes:
            continue
        visited_nodes.add(node_identity)
        document_visits += 1
        if document_visits > MAX_YAML_DOCUMENT_NODES:
            # The document is too complex to inspect safely. Fail closed at its
            # first line rather than silently accepting unchecked YAML.
            findings[1] = "YAML traversal limit exceeded"
            return findings

        if isinstance(node, MappingNode):
            for key_node, value_node in reversed(node.value):
                if isinstance(key_node, ScalarNode):
                    key = key_node.value
                    if YAML_CREDENTIAL_KEY.fullmatch(key) and _yaml_value_is_sensitive(key, value_node):
                        findings[key_node.start_mark.line + 1] = key
                stack.append(value_node)
        elif isinstance(node, SequenceNode):
            stack.extend(reversed(node.value))
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
    normalized_key = re.sub(r"[^a-z0-9]", "", key.rsplit(".", 1)[-1].lower())
    if normalized_key in {"mnemonic", "seed", "seedphrase", "recoveryphrase"}:
        return _is_likely_recovery_phrase(candidate)
    if normalized_key == "privatekey":
        try:
            return _yaml_node_has_private_material(
                ScalarNode("tag:yaml.org,2002:str", candidate)
            )
        except _YamlTraversalLimitExceeded:
            return True
    return re.search(
        r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=.-]{20,}(?![A-Za-z0-9])",
        candidate,
    ) is not None


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
                    if yaml_key in {"mnemonic", "seed", "seedphrase", "recoveryphrase", "privatekey"}:
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
