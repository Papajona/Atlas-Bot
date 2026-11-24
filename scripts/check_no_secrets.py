#!/usr/bin/env python3
"""Repository secret/configuration scanner used by CI and release verification.

This is intentionally conservative: it reports high-signal credential assignments and
private-key/seed material while allowing placeholders and public-key material.
"""
from __future__ import annotations
import ast
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
    r"""(?i)(?<![A-Za-z0-9_])(?:[A-Za-z_][A-Za-z0-9_]*\.)*(?:-\s*)?(?:export\s+)?["']?(?P<key>mnemonic|seed|seed[_-]?phrase|recovery[_-]?phrase|private[_-]?key)["']?\s*[:=]\s*(?P<value>(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')(?:\s*\+\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[A-Za-z_][A-Za-z0-9_.]*))*|[^#\r\n]+)"""
)
DOCUMENTED_RECOVERY_ASSIGNMENT = re.compile(
    r"""(?i)(?:[*_]{1,3})?\b(?P<key>mnemonic|seed(?:[_ -]?phrase)?|recovery[_ -]?phrase)(?:[*_]{1,3})?\s*[:=]\s*(?P<value>[^#\r\n]*)"""
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
# Include JavaScript/TypeScript template literals as well as quoted strings.
SOURCE_STRING_LITERAL = re.compile(
    r"""(?P<quote>["'\x60])(?P<value>(?:\\.|[^\\])*?)\1"""
)

# Match complete Python-style triple-quoted credential assignments before the
# line-oriented scanner sees only the opening pair of quotes.
TRIPLE_QUOTED_SOURCE_ASSIGNMENT = re.compile(
    r"""(?is)(?<![A-Za-z0-9_])(?:[A-Za-z_][A-Za-z0-9_]*\.)*(?:export\s+)?["']?(?P<key>mnemonic|seed|seed[_-]?phrase|recovery[_-]?phrase|private[_-]?key)["']?\s*[:=]\s*(?P<prefix>br|rb|fr|rf|r|u|b|f)?(?P<quote>["']{3})(?P<value>.*?)(?P=quote)"""
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


def _mask_template_interpolations(value: str, _depth: int = 0) -> str:
    """Ignore dynamic template expressions but retain quoted literal material within them."""
    if "$" + "{" not in value:
        return value
    # Bound nested template parsing. At the limit, leave the source visible so
    # conservative secret checks fail closed rather than silently hiding it.
    if _depth >= 64:
        return value

    def skip_quoted(position: int, quote: str) -> int:
        position += 1
        while position < len(value):
            if value[position] == "\\":
                position += 2
            elif value[position] == quote:
                return position + 1
            else:
                position += 1
        return len(value)

    def regex_literal_start(position: int) -> bool:
        prefix = value[:position].rstrip()
        if not prefix:
            return True
        if prefix[-1] in "([{=,:;!?&|+-*%^~<>":
            return True
        return re.search(
            r"\\b(?:return|throw|case|delete|void|typeof|instanceof|in|of|yield|await)\\s*$",
            prefix,
        ) is not None

    def skip_regex_literal(position: int) -> int | None:
        """Skip a JavaScript regex, respecting escapes and slash-containing character classes."""
        position += 1
        in_character_class = False
        while position < len(value):
            char = value[position]
            if char in {"\\n", "\\r"}:
                return None
            if char == "\\\\":
                position += 2
                continue
            if char == "[" and not in_character_class:
                in_character_class = True
            elif char == "]" and in_character_class:
                in_character_class = False
            elif char == "/" and not in_character_class:
                position += 1
                while position < len(value) and value[position].isalpha():
                    position += 1
                return position
            position += 1
        return None

    def skip_interpolation(position: int) -> tuple[int, str]:
        brace_depth = 1
        position += 2
        literal_parts: list[str] = []
        while position < len(value):
            char = value[position]
            if char in {"'", '"'}:
                literal_start = position
                position = skip_quoted(position, char)
                literal_parts.append(value[literal_start + 1:position - 1])
                continue
            if char == chr(96):
                literal_start = position
                position = skip_quoted(position, char)
                nested_template = value[literal_start + 1:position - 1]
                literal_parts.append(
                    _mask_template_interpolations(nested_template, _depth + 1)
                )
                continue
            if char == "/" and regex_literal_start(position):
                regex_end = skip_regex_literal(position)
                if regex_end is not None:
                    position = regex_end
                    continue
                # Invalid or unterminated regex syntax is preserved as literal
                # material through the current line. This prevents malformed
                # patterns from hiding credential-like text behind comment markers.
                newline = value.find("\\n", position)
                literal_end = len(value) if newline < 0 else newline
                literal_parts.append(value[position:literal_end])
                position = literal_end
                continue
            if value.startswith("//", position):
                newline = value.find("\n", position + 2)
                position = len(value) if newline < 0 else newline + 1
                continue
            if value.startswith("/*", position):
                closing = value.find("*/", position + 2)
                position = len(value) if closing < 0 else closing + 2
                continue
            if char == "{":
                brace_depth += 1
                if brace_depth > 256:
                    return len(value), "".join(literal_parts)
            elif char == "}":
                brace_depth -= 1
                if brace_depth == 0:
                    return position + 1, "".join(literal_parts)
            elif char in {"=", ":"}:
                # Preserve assignment punctuation between literal pieces, but
                # discard identifiers and operators from dynamic expressions.
                literal_parts.append(char)
            position += 1
        # An incomplete interpolation is not trusted as literal key material.
        return len(value), "".join(literal_parts)

    pieces: list[str] = []
    segment_start = 0
    position = 0
    while position < len(value):
        if value[position] == "\\":
            # Escaped interpolation syntax is ordinary template text.
            position += 2
            continue
        if value.startswith("$" + "{", position):
            end, literal_material = skip_interpolation(position)
            pieces.extend((value[segment_start:position], " ", literal_material, " "))
            position = end
            segment_start = end
            continue
        position += 1
    pieces.append(value[segment_start:])
    return "".join(pieces)

def _is_inside_template_literal(line: str, position: int) -> bool:
    """Return whether a source assignment begins inside a backtick literal."""
    return any(
        literal.group("quote") == "`"
        and literal.start() < position < literal.end()
        for literal in SOURCE_STRING_LITERAL.finditer(line)
    )


def _has_sensitive_source_literal(key: str, expression: str) -> bool:
    """Detect seed/private-key material in quoted pieces of a source concatenation."""
    components = []
    for match in SOURCE_STRING_LITERAL.finditer(expression):
        component = match.group("value")
        if match.group("quote") == "`":
            component = _mask_template_interpolations(component)
        components.append(component)
    if not components:
        # An identifier-only mnemonic/private-key expression cannot be proven
        # harmless from this line; fail closed rather than trusting unknown values.
        return True

    normalized_key = key.lower().replace("_", "").replace("-", "")
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
    r"""(?i)^(?:[A-Za-z_][A-Za-z0-9_.-]*[._-])?(?:[A-Za-z_][A-Za-z0-9_]*_)?(?:api[_-]?key|api[_-]?secret|secret[_-]?key|private[_-]?key|encryption[_-]?key|signing[_-]?key|service[_-]?role[_-]?key|password|token|mnemonic|seed|seed[_-]?phrase|recovery[_-]?phrase)$"""
)


def _credential_key_kind(key: str) -> str:
    """Return the canonical sensitive-key suffix, including namespaced config keys."""
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    for alias in (
        "recoveryphrase", "seedphrase", "privatekey", "mnemonic", "seed",
        "apikey", "apisecret", "secretkey", "encryptionkey", "signingkey",
        "servicerolekey", "password", "token",
    ):
        if normalized.endswith(alias):
            return alias
    return normalized


RECOVERY_INSTRUCTION_PHRASES = (
    "write down", "store your", "keep your", "never share", "please enter",
    "copy these", "record your", "these words", "your recovery phrase",
    "should store", "must write", "do not share",
)

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
    normalized_text = " ".join(
        word.lower().strip(".,;:!?()[]{}") for word in words
    )
    # Require a phrase-level instruction pattern; a single common word such
    # as "safe" or "share" can legitimately occur in proprietary recovery lists.
    if any(pattern in normalized_text for pattern in RECOVERY_INSTRUCTION_PHRASES):
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


PUBLIC_JWK_METADATA_FIELDS = {
    "n", "e", "x", "y", "crv", "kty", "keyops", "use", "alg", "kid",
    "x5c", "x5t", "x5tu", "x5ts256",
}


def _structured_private_object_has_material(value) -> bool:
    """Detect nested private JWK material while preserving public metadata exceptions."""
    # A root array under private_key has private scalar entries, but objects
    # inside that array may be public JWKs. Do not propagate the array's scalar
    # rule into unknown fields of those objects.
    stack = [(value, isinstance(value, list), isinstance(value, dict))]
    visited: set[tuple[int, bool, bool]] = set()
    visits = 0
    while stack:
        current, scalar_items_are_private, private_context = stack.pop()
        if not isinstance(current, (dict, list)):
            continue
        identity = (id(current), scalar_items_are_private, private_context)
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
                field_is_private = normalized_key in PRIVATE_JWK_MATERIAL_FIELDS
                field_is_public = normalized_key in PUBLIC_JWK_METADATA_FIELDS
                if field_is_private:
                    if isinstance(item, str) and _nonempty_private_scalar(item):
                        return True
                    if isinstance(item, (int, float)) and not isinstance(item, bool) and item != 0:
                        return True
                if isinstance(item, (dict, list)):
                    # Preserve private traversal through containers entered by a
                    # recognized private field (or within an already-private object).
                    # Public JWK members always reset that context.
                    child_private = False if field_is_public else (
                        private_context or field_is_private
                    )
                    child_scalar_items_private = child_private and isinstance(item, list)
                    stack.append((item, child_scalar_items_private, child_private))
        else:
            for item in current:
                if isinstance(item, dict):
                    # The containing array makes direct scalar entries sensitive,
                    # not arbitrary metadata values inside an object element.
                    stack.append((item, False, private_context))
                elif isinstance(item, list):
                    stack.append((item, scalar_items_are_private, private_context))
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
    normalized_key = _credential_key_kind(key)
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
    normalized_key = _credential_key_kind(key)
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


def _find_documented_recovery_assignments(lines: list[str]) -> dict[int, str]:
    """Find inline and indented recovery values in Markdown/text documentation."""
    findings: dict[int, str] = {}
    for index, line in enumerate(lines):
        match = DOCUMENTED_RECOVERY_ASSIGNMENT.search(line)
        if match is None:
            continue

        candidate = (
            match.group("value")
            .strip()
            .strip("`")
            .strip("*_")
            .strip('"')
            .strip("'")
            .strip("`")
            .strip("*_")
            .strip()
        )
        if not candidate:
            key_indent = len(line) - len(line.lstrip(" \t"))
            value_lines: list[str] = []
            for following in lines[index + 1:]:
                if not following.strip():
                    break
                indent = len(following) - len(following.lstrip(" \t"))
                if indent <= key_indent:
                    break
                value_lines.append(
                    following.strip().lstrip(">").lstrip("-* ").strip()
                )
            candidate = " ".join(value_lines).strip()

        if _is_likely_recovery_phrase(candidate):
            findings[index + 1] = match.group("key")
    return findings


def _source_literal_contains_sensitive_assignment(line: str) -> bool:
    """Inspect decoded quoted source snippets for embedded credential assignments."""
    for literal in SOURCE_STRING_LITERAL.finditer(line):
        try:
            # Decode escapes without executing the surrounding source file.
            # This exposes escaped JSON key/value quotes inside JS/Python strings.
            literal_value = ast.literal_eval(literal.group(0))
        except (SyntaxError, ValueError):
            literal_value = literal.group("value")
        if not isinstance(literal_value, str):
            continue
        if literal.group("quote") == "`":
            literal_value = _mask_template_interpolations(literal_value)
        for embedded in TRON_SEED_IN_SOURCE.finditer(literal_value):
            key_kind = _credential_key_kind(embedded.group("key"))
            candidate = embedded.group("value").strip().strip('"').strip("'").strip()
            if key_kind in {"mnemonic", "seed", "seedphrase", "recoveryphrase"}:
                if _is_likely_recovery_phrase(candidate):
                    return True
            elif key_kind == "privatekey":
                if (
                    PRIVATE_KEY.search(candidate)
                    or re.search(
                        r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=.-]{20,}(?![A-Za-z0-9])",
                        candidate,
                    )
                ):
                    return True
    return False


def _find_triple_quoted_source_assignments(source: str) -> set[int]:
    """Return source line numbers containing sensitive triple-quoted assignments."""
    findings: set[int] = set()
    for match in TRIPLE_QUOTED_SOURCE_ASSIGNMENT.finditer(source):
        key_kind = _credential_key_kind(match.group("key"))
        value = match.group("value").strip()
        if key_kind in {"mnemonic", "seed", "seedphrase", "recoveryphrase"}:
            sensitive = _is_likely_recovery_phrase(value)
        elif key_kind == "privatekey":
            if value.startswith(("{", "[")):
                try:
                    structured = json.loads(value)
                    sensitive = _structured_private_object_has_material(structured)
                except (json.JSONDecodeError, RecursionError, _YamlTraversalLimitExceeded):
                    sensitive = True
            else:
                sensitive = (
                    PRIVATE_KEY.search(value) is not None
                    or re.search(
                        r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=.-]{20,}(?![A-Za-z0-9])",
                        value,
                    ) is not None
                )
        else:
            sensitive = False
        if sensitive:
            findings.add(source.count("\n", 0, match.start()) + 1)
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
        elif suffix in {".env", ".toml", ".ini", ".conf", ".cfg", ".json", ".properties"}:
            multiline_seed_findings = _find_multiline_seed_assignments(lines)
        else:
            multiline_seed_findings = {}
        triple_quoted_findings = (
            _find_triple_quoted_source_assignments(text)
            if suffix in SOURCE_CODE_SUFFIXES else set()
        )
        documented_recovery_findings = (
            _find_documented_recovery_assignments(lines)
            if suffix in {".md", ".txt"} else {}
        )
        for lineno,line in enumerate(lines,1):
            if lineno in triple_quoted_findings:
                findings.append(f"{path.relative_to(ROOT)}:{lineno}: seed/private-key assignment")
                continue
            multiline_seed_key = multiline_seed_findings.get(lineno)
            if multiline_seed_key:
                findings.append(f"{path.relative_to(ROOT)}:{lineno}: seed/private-key assignment")
                continue
            documented_recovery_key = documented_recovery_findings.get(lineno)
            if documented_recovery_key:
                findings.append(f"{path.relative_to(ROOT)}:{lineno}: seed/private-key assignment")
                continue
            if PRIVATE_KEY.search(line):
                findings.append(f"{path.relative_to(ROOT)}:{lineno}: private-key material")
                continue
            source_code = path.suffix.lower() in SOURCE_CODE_SUFFIXES
            if source_code and _source_literal_contains_sensitive_assignment(line):
                findings.append(f"{path.relative_to(ROOT)}:{lineno}: seed/private-key assignment")
                continue
            is_yaml = path.suffix.lower() in {".yaml", ".yml"}
            seed_matches = (
                list(TRON_SEED_IN_SOURCE.finditer(line))
                if source_code
                else ([TRON_SEED.search(line)] if not is_yaml or not yaml_parsed else [])
            )
            seed_assignment_found = False
            if "re.compile" not in line:
                # A source object can contain several credential properties on
                # one physical line. Inspect every match so an innocuous first
                # field (for example private_key: "example") cannot hide a later
                # real mnemonic or private-key value.
                for seed_match in seed_matches:
                    if seed_match is None:
                        continue
                    raw_seed_value = seed_match.group("value").strip()
                    yaml_block_scalar = (
                        path.suffix.lower() in {".yaml", ".yml"}
                        and raw_seed_value in {"|", "|-", "|+", ">", ">-", ">+"}
                    )
                    if yaml_block_scalar:
                        # The indented block was already scanned as a whole above;
                        # ignore this marker as seed material, but keep scanning
                        # other credentials that may share the same malformed line.
                        break
                    computed_source_value = (
                        source_code
                        and SOURCE_STRING_CONCATENATION.search(raw_seed_value) is not None
                    )
                    if source_code:
                        inside_template_literal = _is_inside_template_literal(
                            line, seed_match.start()
                        )
                        # In source code, only literal material (including literals
                        # inside concatenations/arrays) is a checked-in secret.
                        # Numeric RNG seeds, parameter defaults, variable references
                        # and runtime key-generation calls are not credential literals.
                        if computed_source_value:
                            sensitive_source_value = _has_sensitive_source_literal(
                                seed_match.group("key"), raw_seed_value
                            )
                        elif SOURCE_STRING_LITERAL.search(raw_seed_value):
                            sensitive_source_value = _has_sensitive_source_literal(
                                seed_match.group("key"), raw_seed_value
                            )
                        else:
                            # TRON_SEED_IN_SOURCE may match an assignment embedded
                            # in a quoted config string (for example
                            # runtime_config = "mnemonic=amber ..."). In that case
                            # raw_seed_value begins after the inner key and may only
                            # have a closing quote. Normalize both ends before the
                            # recovery-phrase/long-token check.
                            bare_source_value = raw_seed_value
                            if inside_template_literal:
                                bare_source_value = _mask_template_interpolations(
                                    bare_source_value
                                )
                            bare_source_value = (
                                bare_source_value.strip().strip("`").strip('"').strip("'").strip()
                            )
                            key_kind = _credential_key_kind(seed_match.group("key"))
                            if key_kind in {"mnemonic", "seed", "seedphrase", "recoveryphrase"}:
                                sensitive_source_value = _is_likely_recovery_phrase(
                                    bare_source_value
                                )
                            else:
                                sensitive_source_value = (
                                    re.fullmatch(
                                        r"[A-Za-z0-9_+/=-]{20,}", bare_source_value
                                    ) is not None
                                    and bare_source_value.lower() not in PLACEHOLDERS
                                    and not ANGLE_PLACEHOLDER.fullmatch(bare_source_value)
                                )
                    else:
                        key_kind = _credential_key_kind(seed_match.group("key"))
                        if key_kind in {"mnemonic", "seed", "seedphrase", "recoveryphrase"}:
                            # A bare "seed" field is also commonly used for numeric
                            # RNG defaults. Only flag actual recovery phrases or
                            # sufficiently long single-token seed material.
                            normalized_seed_value = raw_seed_value.strip().strip('"').strip("'").strip()
                            sensitive_seed_value = _is_likely_recovery_phrase(
                                normalized_seed_value
                            )
                        else:
                            seed_value = raw_seed_value.strip('"').strip("'").strip().lower()
                            sensitive_seed_value = (
                                seed_value not in PLACEHOLDERS
                                and not ANGLE_PLACEHOLDER.fullmatch(seed_value)
                            )
                        sensitive_source_value = sensitive_seed_value
                    if sensitive_source_value:
                        findings.append(f"{path.relative_to(ROOT)}:{lineno}: seed/private-key assignment")
                        seed_assignment_found = True
                        break
            if seed_assignment_found:
                continue
            for match in ASSIGNMENT.finditer(line):
                if is_yaml and yaml_parsed:
                    yaml_key = _credential_key_kind(match.group("key"))
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
