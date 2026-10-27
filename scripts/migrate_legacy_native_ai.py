#!/usr/bin/env python3
"""Semantic provider-removal migration for divergent Atlas legacy branches.

This edits provider-specific functions/settings rather than replaying a broad commit
whose unrelated hunks collide with each branch. It deliberately refuses uncertain edits.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

ROOT = Path.cwd()
DEFAULT_COMMIT = "f5e303dad1a93ce1a33940b3e2b16d118897ebf6"
DEFAULT_EXTRA_BASE_COMMIT = "0d38f95079833804713fcba080caee38a6ddfb4c"
PROVIDER_RE = re.compile(
    r"(?i)(?:gemini|groq|cerebras|ai[\s_-]*gateway|generativelanguage\.googleapis\.com|"
    r"api\.groq\.com|api\.cerebras\.ai|api\.openai\.com|"
    r"AI_GATEWAY_[A-Z0-9_]+|CEREBRAS_[A-Z0-9_]+|OPENAI_[A-Z0-9_]+|openai-compatible|"
    r"GEMINI_API_KEY|GROQ_API_KEY|SECRET_GEMINI|SECRET_GROQ|_gemini_json|_groq_json|"
    r"settings\.(?:gemini|groq|cerebras|ai_gateway)_)")
PROVIDER_FIELD_RE = re.compile(
    r"(?i)(?:gemini|groq|cerebras|ai[\s_-]*gateway|generativelanguage\.googleapis\.com|"
    r"api\.groq\.com|api\.cerebras\.ai|api\.openai\.com|"
    r"AI_GATEWAY_[A-Z0-9_]+|CEREBRAS_[A-Z0-9_]+|OPENAI_[A-Z0-9_]+|openai-compatible|_gemini_json|_groq_json)"
)
KEY_ASSIGNMENT_RE = re.compile(
    r"""(?ix)\b(?:SECRET_)?(?:GEMINI|GROQ|CEREBRAS|AI_GATEWAY|OPENAI)[A-Z0-9_]*\b
    \s*[:=]\s*(?:\$\{\{[^}]+\}\}|"[^"]*"|'[^']*'|[^,\s"']+)"""
)
SECRET_VERSION_RE = re.compile(
    r"(?i)\bSECRET_(?:GEMINI|GROQ|CEREBRAS|AI_GATEWAY|OPENAI)[A-Z0-9_]*\s*:\s*\$\{\{[^}]+\}\}"
)
HOST_RE = re.compile(r"""(?i)https?://(?:generativelanguage\.googleapis\.com|api\.groq\.com|api\.cerebras\.ai|api\.openai\.com)[^\s"']*""")
MODEL_RE = re.compile(r"(?i)\bgemini-[A-Za-z0-9_.-]+\b")

EXTRA_PATHS = (
    "ENTRY_EXIT_3_9_0.md",
    "app/src/main/java/com/atlas/trading/network/ReleaseAdvisorClient.kt",
    "app/src/main/java/com/atlas/trading/ui/ReleaseAdvisorScreen.kt",
)
NATIVE_PATHS = (
    ".env.example",
    ".github/workflows/production-deploy.yml",
    ".github/workflows/production-verification.yml",
    "ENTRY_EXIT_3_9_0.md",
    "app/ai_providers.py",
    "app/config.py",
    "app/daily_research.py",
    "app/main.py",
    "app/native_ai.py",
    "app/src/main/java/com/atlas/trading/network/ReleaseAdvisorClient.kt",
    "app/src/main/java/com/atlas/trading/ui/ReleaseAdvisorScreen.kt",
    "app/strategy_ai.py",
    "app/templates/customer.html",
    "deploy/DEPLOY_WORKFLOW.md",
    "deploy/cloud-run-deploy.sh",
    "deploy/cloud-run-worker-deploy.sh",
    "deploy/cloud-run.env.example",
    "deploy/gcp-bootstrap.sh",
    "metadata.json",
    "scripts/check_ai_models.py",
    "scripts/check_native_ai.py",
    "scripts/verify_release.sh",
)
ANCILLARY_PATHS = tuple(p for p in NATIVE_PATHS if p not in {
    "app/ai_providers.py", "app/config.py", "app/daily_research.py",
    "app/main.py", "app/native_ai.py", "app/strategy_ai.py", "scripts/check_ai_models.py",
    "scripts/check_native_ai.py",
})


class MigrationError(RuntimeError):
    pass


def _git(*args: str, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, input=input_text, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if check and result.returncode:
        raise MigrationError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def canonical_text(commit: str, path: str) -> str:
    result = _git("show", f"{commit}:{path}", check=False)
    if result.returncode:
        raise MigrationError(f"canonical commit {commit} has no readable {path}")
    return result.stdout


def write_text(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _name(node: ast.AST) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                return target.id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def _apply_line_edits(source: str, edits: list[tuple[int, int, str]]) -> str:
    lines = source.splitlines(keepends=True)
    for first, last, replacement in sorted(edits, key=lambda item: item[0], reverse=True):
        lines[first - 1:last] = replacement.splitlines(keepends=True) if replacement else []
    return "".join(lines)


def transform_config() -> None:
    path = ROOT / "app/config.py"
    if not path.is_file():
        raise MigrationError("app/config.py is absent")
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise MigrationError(f"app/config.py cannot be parsed before provider cleanup: {exc}") from exc

    def target_names(target: ast.AST) -> set[str]:
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, (ast.Tuple, ast.List)):
            return {name for item in target.elts for name in target_names(item)}
        return set()

    settings_classes = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name.lower() in {"settings", "appsettings", "atlassettings"}
    ]
    direct_setting_assignment_ids = {
        id(statement)
        for cls in settings_classes
        for statement in cls.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
    }
    statements = list(ast.walk(tree))
    assignments = [node for node in statements if isinstance(node, (ast.Assign, ast.AnnAssign))]
    provider_nodes: list[ast.AST] = []
    removable_lines: set[int] = set()

    def is_provider_field(name: str) -> bool:
        return bool(PROVIDER_FIELD_RE.search(name))

    for node in assignments:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = {name for target in targets for name in target_names(target)}
        provider_names = {name for name in names if is_provider_field(name)}
        if not provider_names:
            continue
        if id(node) not in direct_setting_assignment_ids:
            raise MigrationError(
                "app/config.py contains a provider-named assignment outside the Settings class; refusing to drop helper behavior"
            )
        if names != provider_names:
            raise MigrationError(
                "app/config.py has a mixed provider/unrelated assignment; refusing to remove unrelated settings"
            )
        provider_nodes.append(node)
        removable_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    statement_types = (
        ast.Assign, ast.AnnAssign, ast.Expr, ast.Import, ast.ImportFrom,
        ast.Raise, ast.Return, ast.Assert, ast.AugAssign, ast.Delete,
    )
    for provider_node in provider_nodes:
        start = provider_node.lineno
        end = provider_node.end_lineno or start
        for other in statements:
            if other is provider_node or other in provider_nodes or not isinstance(other, statement_types):
                continue
            other_start = getattr(other, "lineno", None)
            other_end = getattr(other, "end_lineno", None) or other_start
            if other_start is not None and other_end is not None and other_start <= end and start <= other_end:
                raise MigrationError(
                    "app/config.py has a provider field sharing a physical line with a non-provider statement; refusing lossy cleanup"
                )

    lines = source.splitlines(keepends=True)
    result = "".join(line for number, line in enumerate(lines, start=1) if number not in removable_lines)
    result = "".join(
        line for line in result.splitlines(keepends=True)
        if not (line.lstrip().startswith("#") and PROVIDER_RE.search(line))
    )
    if PROVIDER_RE.search(result):
        raise MigrationError(
            "app/config.py still contains provider text outside isolated Settings fields; refusing lossy cleanup"
        )
    ast.parse(result, filename=str(path))
    path.write_text(result, encoding="utf-8")


def _defined_names(node: ast.AST) -> set[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Assign):
        return {name for target in node.targets for name in _assigned_names(target)}
    if isinstance(node, ast.AnnAssign):
        return _assigned_names(node.target)
    if isinstance(node, ast.Import):
        return {alias.asname or alias.name.split(".", 1)[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom):
        return {alias.asname or alias.name for alias in node.names if alias.name != "*"}
    return set()


def _assigned_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return {name for item in target.elts for name in _assigned_names(item)}
    return set()


def transform_ai_providers(commit: str) -> None:
    """Replace hosted adapters with the Native AI facade while retaining unrelated helpers/imports."""
    path = ROOT / "app/ai_providers.py"
    if not path.is_file():
        write_text("app/ai_providers.py", canonical_text(commit, "app/ai_providers.py"))
        return
    source = path.read_text(encoding="utf-8")
    if not PROVIDER_RE.search(source):
        return

    old_tree = ast.parse(source, filename=str(path))
    canonical = canonical_text(commit, "app/ai_providers.py")
    canonical_tree = ast.parse(canonical, filename="canonical app/ai_providers.py")
    canonical_nodes_by_name: dict[str, ast.AST] = {}
    for canonical_node in canonical_tree.body:
        for defined_name in _defined_names(canonical_node):
            canonical_nodes_by_name.setdefault(defined_name, canonical_node)
    canonical_names = set(canonical_nodes_by_name)
    provider_adapter_names = {
        "_gemini_json", "_groq_json", "_prompt",
        "dual_ai_research_review", "dual_ai_trade_safety_review",
    }
    extras: list[str] = []
    import_nodes: list[ast.AST] = []
    source_lines = source.splitlines(keepends=True)
    for node in old_tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_nodes.append(node)
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        name = _name(node)
        if name in provider_adapter_names:
            continue
        start_line = min([node.lineno, *[decorator.lineno for decorator in getattr(node, "decorator_list", [])]])
        segment = "".join(source_lines[start_line - 1:node.end_lineno]).rstrip("\n")
        if name and name in canonical_names:
            canonical_node = canonical_nodes_by_name[name]
            if ast.dump(node, include_attributes=False) == ast.dump(canonical_node, include_attributes=False):
                continue
            raise MigrationError(
                f"app/ai_providers.py has branch-specific definition {name!r} that collides with a canonical name; refusing to discard or shadow it"
            )
        if PROVIDER_RE.search(segment):
            raise MigrationError(
                f"app/ai_providers.py contains an unrecognized provider-bearing helper {name or type(node).__name__}; refusing to overwrite it"
            )
        extras.append(segment.rstrip())

    # Preserve only imports that retained branch helpers actually use and that the
    # canonical facade does not already provide. Never carry hosted-provider imports
    # solely because removed adapters previously needed them.
    used_names: set[str] = set()
    for segment in extras:
        tree = ast.parse(segment)
        used_names.update(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))
    preserved_imports: list[str] = []
    for node in import_nodes:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        aliases = []
        for alias in node.names:
            bound = alias.asname or (
                alias.name.split(".", 1)[0] if isinstance(node, ast.Import) else alias.name
            )
            if bound not in used_names or bound in canonical_names or PROVIDER_RE.search(alias.name):
                continue
            aliases.append(alias)
        if not aliases:
            continue
        if isinstance(node, ast.Import):
            preserved_imports.append(ast.unparse(ast.Import(names=aliases)))
        else:
            preserved_imports.append(ast.unparse(ast.ImportFrom(
                module=node.module, names=aliases, level=node.level
            )))

    pieces = [canonical.rstrip()]
    if preserved_imports:
        pieces.append("\n".join(sorted(set(preserved_imports))))
    if extras:
        pieces.extend(extras)
    result = "\n\n".join(piece for piece in pieces if piece.strip()) + "\n"
    ast.parse(result, filename=str(path))
    if PROVIDER_RE.search(result):
        raise MigrationError("app/ai_providers.py still contains hosted-provider wiring after facade migration")
    path.write_text(result, encoding="utf-8")


def transform_strategy_ai(commit: str) -> None:
    path = ROOT / "app/strategy_ai.py"
    if not path.is_file():
        raise MigrationError("app/strategy_ai.py is absent")
    source = path.read_text(encoding="utf-8")
    if not PROVIDER_RE.search(source):
        return
    canonical = canonical_text(commit, "app/strategy_ai.py")
    old_tree = ast.parse(source, filename=str(path))
    new_tree = ast.parse(canonical, filename="canonical app/strategy_ai.py")

    removed_names = {"SYSTEM_PROMPT", "_strategy_prompt", "_generate_with_provider", "generate_strategy_draft"}
    definitions_by_name = {
        name: [node for node in old_tree.body if _name(node) == name]
        for name in removed_names
    }
    duplicate_definitions = sorted(
        name for name, nodes in definitions_by_name.items() if len(nodes) > 1
    )
    if duplicate_definitions:
        raise MigrationError(
            "strategy module has multiple top-level definitions for removed names: "
            + ", ".join(duplicate_definitions)
        )

    generator_definitions = definitions_by_name["generate_strategy_draft"]
    if len(generator_definitions) != 1 or not isinstance(generator_definitions[0], ast.AsyncFunctionDef):
        raise MigrationError(
            "strategy module must have exactly one top-level async generate_strategy_draft definition"
        )
    legacy_generate = generator_definitions[0]
    body = legacy_generate.body
    safe_trampoline = (
        not legacy_generate.decorator_list
        and len(body) == 1
        and isinstance(body[0], ast.Return)
        and isinstance(body[0].value, ast.Await)
        and isinstance(body[0].value.value, ast.Call)
        and isinstance(body[0].value.value.func, ast.Name)
        and body[0].value.value.func.id == "_generate_with_provider"
    )
    if not safe_trampoline:
        raise MigrationError(
            "generate_strategy_draft contains branch-specific behavior beyond the recognized provider trampoline; refusing to overwrite it"
        )

    provider_definitions = definitions_by_name["_generate_with_provider"]
    if len(provider_definitions) != 1 or not isinstance(provider_definitions[0], ast.AsyncFunctionDef):
        raise MigrationError(
            "strategy module must have exactly one top-level async _generate_with_provider helper; refusing to drop unknown behavior"
        )
    legacy_provider = provider_definitions[0]
    if legacy_provider.decorator_list:
        raise MigrationError(
            "strategy module has a decorated _generate_with_provider helper; refusing to drop unknown behavior"
        )
    provider_body = legacy_provider.body
    provider_delegations = {"_gemini_json", "_groq_json", "_cerebras_json", "_openai_json"}
    direct_provider_trampoline = (
        len(provider_body) == 1
        and isinstance(provider_body[0], ast.Return)
        and isinstance(provider_body[0].value, ast.Await)
        and isinstance(provider_body[0].value.value, ast.Call)
        and (
            (isinstance(provider_body[0].value.value.func, ast.Name)
             and provider_body[0].value.value.func.id in provider_delegations)
            or (isinstance(provider_body[0].value.value.func, ast.Attribute)
                and provider_body[0].value.value.func.attr in provider_delegations)
        )
    )
    if not direct_provider_trampoline:
        raise MigrationError(
            "_generate_with_provider contains additional or unknown behavior; refusing to discard its body"
        )
    canonical_generate = next(
        (node for node in new_tree.body
         if isinstance(node, ast.AsyncFunctionDef) and node.name == "generate_strategy_draft"),
        None,
    )

    if canonical_generate is None:
        raise MigrationError("canonical strategy module lacks generate_strategy_draft")
    canonical_lines = canonical.splitlines(keepends=True)
    new_function = "".join(canonical_lines[canonical_generate.lineno - 1:canonical_generate.end_lineno])

    ai_provider_imports: list[ast.ImportFrom] = []
    retained_nodes: list[ast.AST] = []
    imported_symbols_by_bound_name: dict[str, set[str]] = {}
    for node in old_tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "ai_providers":
            ai_provider_imports.append(node)
            for alias in node.names:
                if alias.name == "*":
                    raise MigrationError(
                        "strategy module uses a wildcard .ai_providers import; refusing to guess retained helper dependencies"
                    )
                bound_name = alias.asname or alias.name
                imported_symbols_by_bound_name.setdefault(bound_name, set()).add(alias.name)
            continue
        name = _name(node)
        if name in removed_names:
            continue
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            continue
        retained_nodes.append(node)

    retained_name_uses = {
        part.id
        for node in retained_nodes
        for part in ast.walk(node)
        if isinstance(part, ast.Name)
    }
    used_removed_imports = set(imported_symbols_by_bound_name) & retained_name_uses
    native_symbols = {"NativeAIError", "compose_strategy_spec"}
    unsafe_imports: list[str] = []
    for bound_name in sorted(used_removed_imports):
        original_names = imported_symbols_by_bound_name[bound_name]
        if bound_name not in native_symbols or original_names != {bound_name}:
            original_text = ", ".join(sorted(original_names))
            unsafe_imports.append(f"{original_text} bound as {bound_name}")
    if unsafe_imports:
        raise MigrationError(
            "retained strategy helper depends on non-native or aliased .ai_providers import(s) "
            + ", ".join(unsafe_imports)
            + "; only imports whose original symbol matches NativeAIError or compose_strategy_spec can be remapped"
        )

    edits: list[tuple[int, int, str]] = []
    found_generate = False
    lines = source.splitlines(keepends=True)
    replacement_import_inserted = False
    for node in old_tree.body:
        name = _name(node)
        if name in {"SYSTEM_PROMPT", "_strategy_prompt", "_generate_with_provider"}:
            start_line = min(
                [node.lineno, *[decorator.lineno for decorator in getattr(node, "decorator_list", [])]]
            )
            edits.append((start_line, node.end_lineno, ""))
        elif isinstance(node, ast.AsyncFunctionDef) and name == "generate_strategy_draft":
            edits.append((node.lineno, node.end_lineno, new_function))
            found_generate = True

    for node in ai_provider_imports:
        replacement = ""
        if not replacement_import_inserted:
            replacement = "from .native_ai import NativeAIError, compose_strategy_spec\n"
            replacement_import_inserted = True
        edits.append((node.lineno, node.end_lineno, replacement))

    if not found_generate or not ai_provider_imports:
        raise MigrationError("strategy module layout is unexpected; refusing lossy rewrite")
    result = _apply_line_edits(source, edits)
    if PROVIDER_RE.search(result):
        raise MigrationError("strategy module still contains provider references")
    ast.parse(result, filename=str(path))
    path.write_text(result, encoding="utf-8")


def transform_daily_research() -> None:
    path = ROOT / "app/daily_research.py"
    if not path.is_file():
        raise MigrationError("app/daily_research.py is absent")
    source = path.read_text(encoding="utf-8")
    if not PROVIDER_RE.search(source):
        return
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines(keepends=True)
    edits: list[tuple[int, int, str]] = []
    found_callsite = False
    provider_delegations = {"dual_ai_research_review", "_gemini_json", "_groq_json", "_cerebras_json"}

    def contains_provider_call(statement: ast.AST) -> bool:
        return any(
            isinstance(part, ast.Call)
            and ((isinstance(part.func, ast.Name) and part.func.id in provider_delegations)
                 or (isinstance(part.func, ast.Attribute) and part.func.attr in provider_delegations))
            for part in ast.walk(statement)
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test_segment = ast.get_source_segment(source, node.test) or ""
        if not re.search(r"(?i)(?:gemini_api_key|groq_api_key|cerebras_api_key|ai_gateway_)", test_segment):
            continue
        def is_review_result_target(target: ast.AST) -> bool:
            if not isinstance(target, ast.Subscript) or not isinstance(target.value, ast.Name):
                return False
            if target.value.id != "review" or not isinstance(target.slice, ast.Constant):
                return False
            return target.slice.value in {"external_ai", "ai_review", "external_ai_review"}

        def is_provider_expression(expression: ast.AST | None) -> bool:
            if isinstance(expression, ast.Await):
                expression = expression.value
            if not isinstance(expression, ast.Call):
                return False
            # Don't erase nested setup/configuration calls inside provider arguments.
            if sum(1 for part in ast.walk(expression) if isinstance(part, ast.Call)) != 1:
                return False
            return (
                (isinstance(expression.func, ast.Name) and expression.func.id in provider_delegations)
                or (isinstance(expression.func, ast.Attribute) and expression.func.attr in provider_delegations)
            )

        def is_provider_only_statement(statement: ast.AST) -> bool:
            if isinstance(statement, ast.Assign):
                return (len(statement.targets) == 1
                        and is_review_result_target(statement.targets[0])
                        and is_provider_expression(statement.value))
            if isinstance(statement, ast.AnnAssign):
                return is_review_result_target(statement.target) and is_provider_expression(statement.value)
            # Returning from the enclosing routine changes control flow. Expression-only calls
            # and arbitrary statements may contain side effects, so unknown shapes must fail closed.
            return False

        def is_not_configured_fallback(statement: ast.AST) -> bool:
            if isinstance(statement, ast.Assign):
                if len(statement.targets) != 1 or not is_review_result_target(statement.targets[0]):
                    return False
            elif isinstance(statement, ast.AnnAssign):
                if not is_review_result_target(statement.target):
                    return False
            else:
                return False
            value = statement.value
            if not isinstance(value, ast.Dict) or len(value.keys) != 1 or len(value.values) != 1:
                return False
            key, item = value.keys[0], value.values[0]
            return (
                isinstance(key, ast.Constant) and key.value == "status"
                and isinstance(item, ast.Constant) and item.value == "NOT_CONFIGURED"
            )

        body_is_provider_only = bool(node.body) and all(
            is_provider_only_statement(statement) for statement in node.body
        )
        else_is_provider_fallback = all(
            is_provider_only_statement(statement) or is_not_configured_fallback(statement)
            for statement in node.orelse
        )
        if not body_is_provider_only or not else_is_provider_fallback:
            raise MigrationError(
                "daily-research provider conditional contains unrelated or unknown logic; refusing to replace its whole block"
            )
        first = lines[node.lineno - 1]
        indent = first[:len(first) - len(first.lstrip())]
        replacement = "\n".join((
            indent + "native_research_input = dict(research)",
            indent + 'native_research_input["paper_candidates"] = review.get("paper_candidates", research.get("paper_candidates", []))',
            indent + 'review["native_ai_review"] = native_research_review(native_research_input, intelligence)',
        )) + "\n"
        edits.append((node.lineno, node.end_lineno, replacement))
        found_callsite = True
        break
    for index, line in enumerate(lines, start=1):
        if re.search(r"^\s*from\s+\.ai_providers\s+import\s+dual_ai_research_review\b", line):
            edits.append((index, index, "from .native_ai import native_research_review\n"))
    if not found_callsite:
        raise MigrationError("daily-research provider callsite is unexpected; refusing lossy rewrite")
    result = _apply_line_edits(source, edits)
    if PROVIDER_RE.search(result):
        raise MigrationError("daily research still contains provider references")
    ast.parse(result, filename=str(path))
    path.write_text(result, encoding="utf-8")



def _release_diagnostics_block(source: str, filename: str) -> tuple[int, int, str]:
    """Return zero-based start, exclusive end and text for the known release diagnostics block."""
    lines = source.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.strip() == "# Authorized release diagnostics. The gateway key is read only from server configuration."]
    if len(starts) != 1:
        raise MigrationError(f"{filename} does not have one known release-diagnostics block")
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise MigrationError(f"{filename} cannot be parsed before release-diagnostics migration: {exc}") from exc
    route = next(
        (node for node in tree.body
         if isinstance(node, ast.AsyncFunctionDef) and node.name == "admin_release_diagnostics"),
        None,
    )
    if route is None or route.end_lineno is None or route.lineno <= starts[0] + 1:
        raise MigrationError(f"{filename} has an unexpected admin_release_diagnostics route")
    block = "".join(lines[starts[0]:route.end_lineno])
    return starts[0], route.end_lineno, block


def transform_release_diagnostics(extra_commit: str) -> None:
    """Replace only the known hosted release-diagnostics route, preserving other branch endpoints."""
    path = ROOT / "app/main.py"
    if not path.is_file():
        return
    source = path.read_text(encoding="utf-8")
    markers = ("settings.ai_gateway_api_key", "settings.ai_gateway_base_url", "/chat/completions")
    if not any(marker in source for marker in markers):
        return
    start, end, old_block = _release_diagnostics_block(source, "app/main.py")
    if not any(marker in old_block for marker in markers):
        raise MigrationError(
            "app/main.py has hosted gateway wiring outside the recognized release-diagnostics block; refusing rewrite"
        )

    canonical = canonical_text(extra_commit, "app/main.py")
    _, _, new_block = _release_diagnostics_block(canonical, "canonical app/main.py")
    if "deterministic-local-rules-v1" not in new_block or "_release_diagnostic_report" not in new_block:
        raise MigrationError("canonical release-diagnostics block is not the reviewed local-rules implementation")
    if any(marker in new_block for marker in markers):
        raise MigrationError("canonical release-diagnostics block still contains a hosted gateway call")

    lines = source.splitlines(keepends=True)
    lines[start:end] = new_block.splitlines(keepends=True)
    result = "".join(lines)
    ast.parse(result, filename=str(path))
    path.write_text(result, encoding="utf-8")


def _remove_empty_env(lines: list[str]) -> list[str]:
    result = list(lines)
    changed = True
    while changed:
        changed = False
        for index, line in enumerate(result):
            match = re.match(r"^(\s*)env:\s*(?:#.*)?(?:\r?\n)?$", line)
            if not match:
                continue
            indent = len(match.group(1))
            has_child = False
            for later in result[index + 1:]:
                if not later.strip() or later.lstrip().startswith("#"):
                    continue
                later_indent = len(later) - len(later.lstrip())
                if later_indent <= indent:
                    break
                has_child = True
                break
            if not has_child:
                del result[index]
                changed = True
                break
    return result


def scrub_shell_deployment(path: str, text: str) -> str:
    """Remove provider-specific shell wiring without corrupting bundled env/secret lists."""
    output: list[str] = []
    skip_provider_block = False
    variable_pattern = re.compile(
        r"(?i)\b(?:SECRET_GEMINI[A-Z0-9_]*|SECRET_GROQ[A-Z0-9_]*|"
        r"GEMINI_API_KEY|GROQ_API_KEY|GROQ_SECRET_ARG|AI_SECRET_ARGS|"
        r"CEREBRAS_[A-Z0-9_]*|AI_GATEWAY_[A-Z0-9_]*|OPENAI_[A-Z0-9_]*|OPENAI_SECRET_ARG)\b"
    )
    for original in text.splitlines(keepends=True):
        line = original
        stripped = line.lstrip()
        if skip_provider_block:
            if re.match(r"^\s*fi\b", line):
                skip_provider_block = False
                continue
            if (not stripped or stripped.startswith("#")
                    or re.match(r"^(?:else|elif\b|then\b)", stripped, re.I)):
                continue
            provider_assignment = re.match(
                r"^\s*(?:export\s+)?(?:SECRET_GEMINI[A-Z0-9_]*|SECRET_GROQ[A-Z0-9_]*|"
                r"GEMINI_API_KEY|GROQ_API_KEY|GROQ_SECRET_ARG|AI_SECRET_ARGS|"
                r"CEREBRAS_[A-Z0-9_]*|AI_GATEWAY_[A-Z0-9_]*|OPENAI_[A-Z0-9_]*|OPENAI_SECRET_ARG)\s*=",
                line, re.I,
            )
            warning_only = (
                re.match(r"^\s*(?:echo|printf)\b", stripped, re.I)
                and PROVIDER_RE.search(stripped)
                and re.search(r"(?i)\b(?:warn(?:ing)?|not configured|missing|unset|skipping|optional)\b", stripped)
            )
            if provider_assignment or warning_only:
                continue
            raise MigrationError(
                f"{path} has a mixed provider-conditioned shell block; refusing to discard unrelated statement: {stripped[:100]}"
            )

        # Drop only a provider-specific if/else block, including its warning body.
        if (re.match(r"^\s*if\b", line) and variable_pattern.search(line)
                and not re.search(r";\s*fi\b", line)):
            skip_provider_block = True
            continue

        # Provider-only shell declarations/guards/secret commands have no unrelated payload.
        if re.match(r"^\s*(?::\s*)?['\"]?\$\{?(?:SECRET_GEMINI|GEMINI_API_KEY|GROQ_API_KEY)", line, re.I):
            continue
        if re.match(
            r"^\s*(?:SECRET_GEMINI[A-Z0-9_]*|SECRET_GROQ[A-Z0-9_]*|"
            r"GEMINI_API_KEY|GROQ_API_KEY|GROQ_SECRET_ARG|AI_SECRET_ARGS|"
            r"CEREBRAS_API_KEY|AI_GATEWAY_API_KEY)\s*=",
            line, re.I,
        ):
            continue
        if re.search(r"(?i)\b(?:create_secret|grant_secret_access)\s+atlas-(?:gemini|groq|cerebras|ai-gateway|openai)\b", line):
            continue
        if stripped.startswith("#") and PROVIDER_RE.search(line):
            continue

        # Remove provider entries from combined Cloud Run env-var lists.
        if "ENV_VARS=" in line:
            line = re.sub(
                r"@(?:GEMINI_MODEL|GEMINI_STRATEGY_MODEL|GROQ_MODEL|GROQ_STRATEGY_MODEL|"
                r"GROQ_MAX_COMPLETION_TOKENS|CEREBRAS_[A-Z0-9_]+|AI_GATEWAY_[A-Z0-9_]+|OPENAI_[A-Z0-9_]+)=[^@\"]*",
                "",
                line,
                flags=re.I,
            )
        if "--set-env-vars" in line:
            line = re.sub(
                r",?(?:GEMINI_MODEL|GEMINI_STRATEGY_MODEL|GROQ_MODEL|GROQ_STRATEGY_MODEL|"
                r"GROQ_MAX_COMPLETION_TOKENS|CEREBRAS_[A-Z0-9_]+|AI_GATEWAY_[A-Z0-9_]+|OPENAI_[A-Z0-9_]+)=[^,\"]*",
                "",
                line,
                flags=re.I,
            )
        if "--set-secrets" in line:
            line = re.sub(
                r",?(?:GEMINI_API_KEY|GROQ_API_KEY|CEREBRAS_API_KEY|AI_GATEWAY_API_KEY|OPENAI_API_KEY)=[^,\"]*",
                "",
                line,
                flags=re.I,
            )
            line = re.sub(r",?\$\{(?:AI_SECRET_ARGS|GROQ_SECRET_ARG)\}", "", line)
        # Provider secret names in bootstrap allowlists are independent list items.
        if "for secret in" in line:
            line = re.sub(
                r"\s+atlas-(?:gemini|groq|cerebras|ai-gateway|openai)[-a-z0-9]*",
                "",
                line,
                flags=re.I,
            )
        # Strip any remaining provider-only executable line. If a mixed executable line
        # still contains a provider token, refuse the branch rather than damage its command.
        if PROVIDER_RE.search(line):
            if stripped.startswith("#"):
                continue
            warning_only = (
                re.match(r"^\s*(?:echo|printf)\b", stripped, re.I)
                and re.search(r"(?i)\b(?:gemini|groq|cerebras|ai.?gateway|openai)\b", stripped)
                and re.search(r"(?i)\b(?:warn(?:ing)?|not configured|missing|unset|skipping|optional)\b", stripped)
            )
            if warning_only:
                continue
            inline_if = re.match(r"^\s*if\b.*\bthen\b(.*);\s*fi\s*$", line, re.I)
            if inline_if:
                commands = [part.strip() for part in inline_if.group(1).split(";") if part.strip()]
                safe_inline = all(
                    re.match(
                        r"^(?::|(?:export\s+)?(?:SECRET_GEMINI[A-Z0-9_]*|SECRET_GROQ[A-Z0-9_]*|"
                        r"GEMINI_API_KEY|GROQ_API_KEY|GROQ_SECRET_ARG|AI_SECRET_ARGS|"
                        r"CEREBRAS_[A-Z0-9_]*|AI_GATEWAY_[A-Z0-9_]*|OPENAI_[A-Z0-9_]*|OPENAI_SECRET_ARG)\s*=)",
                        command, re.I,
                    )
                    or (
                        re.match(r"^(?:echo|printf)\b", command, re.I)
                        and re.search(r"(?i)\b(?:gemini|groq|cerebras|ai.?gateway|openai)\b", command)
                        and re.search(r"(?i)\b(?:warn(?:ing)?|not configured|missing|unset|skipping|optional)\b", command)
                    )
                    for command in commands
                )
                if safe_inline:
                    continue
                raise MigrationError(
                    f"{path} has a mixed inline provider conditional; refusing to discard unrelated commands"
                )
            if re.search(r"(?i)\b(?:gcloud|docker|curl|python|bash|run:|ENV_VARS=|--set-env-vars|--set-secrets)\b", line):
                raise MigrationError(
                    f"{path} retains provider wiring in a mixed shell command; refusing to rewrite it"
                )
            raise MigrationError(
                f"{path} retains provider text in a non-provider shell statement; refusing lossy deletion"
            )
        if line.strip():
            output.append(line)
        elif original.strip():
            output.append(line)

    if skip_provider_block:
        raise MigrationError(f"{path} has an unterminated provider-specific shell block")
    cleaned = "".join(output)
    if PROVIDER_RE.search(cleaned):
        raise MigrationError(f"{path} still contains hosted-provider wiring after shell cleanup")
    return cleaned


def scrub_ancillary_text(path: str, text: str) -> str:
    if path in {"deploy/cloud-run-deploy.sh", "deploy/cloud-run-worker-deploy.sh", "deploy/gcp-bootstrap.sh"}:
        return scrub_shell_deployment(path, text)
    if path == "metadata.json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MigrationError(f"metadata.json is invalid JSON: {exc}") from exc

        def scrub(value, key_context=""):
            if isinstance(value, dict):
                return {
                    k: scrub(v, str(k))
                    for k, v in value.items()
                    if not PROVIDER_FIELD_RE.search(str(k))
                }
            if isinstance(value, list):
                return [scrub(item, key_context) for item in value]
            if isinstance(value, str) and PROVIDER_RE.search(value):
                if re.fullmatch(r"(?i)(?:[a-z0-9]+_)*model(?:_(?:id|name|version))?", key_context):
                    return "atlas-native-strategy-v1"
                cleaned = SECRET_VERSION_RE.sub("", value)
                cleaned = KEY_ASSIGNMENT_RE.sub("", cleaned)
                cleaned = HOST_RE.sub("", cleaned)
                cleaned = MODEL_RE.sub("atlas-native-strategy-v1", cleaned)
                return PROVIDER_RE.sub("legacy provider", cleaned)
            return value

        return json.dumps(scrub(data), indent=2, ensure_ascii=False) + "\n"

    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        if not PROVIDER_RE.search(line):
            kept.append(line)
            continue
        candidate = SECRET_VERSION_RE.sub("", line)
        candidate = KEY_ASSIGNMENT_RE.sub("", candidate)
        candidate = HOST_RE.sub("", candidate)
        candidate = MODEL_RE.sub("atlas-native-strategy-v1", candidate)
        candidate = re.sub(r"(?i)\b(?:Gemini|Groq|Cerebras|AI Gateway)\b", "", candidate)
        if PROVIDER_RE.search(candidate):
            if re.search(r"(?i)\b(?:gcloud|docker|curl|python|bash|run:)\b", candidate):
                raise MigrationError(f"{path} has provider wiring inside an executable line; refusing automatic deletion")
            candidate = PROVIDER_RE.sub("legacy provider", candidate)
        if candidate.strip():
            kept.append(candidate)
    cleaned = "".join(_remove_empty_env(kept))
    if PROVIDER_RE.search(cleaned):
        raise MigrationError(f"{path} still contains provider references")
    if path == "metadata.json":
        json.loads(cleaned)
    return cleaned


def _apply_path_patch(base_commit: str, tip_commit: str, path: str) -> bool:
    patch = _git("diff", "--binary", base_commit, tip_commit, "--", path).stdout
    if not patch:
        return True
    applied = _git("apply", "--3way", "--index", input_text=patch, check=False)
    if applied.returncode:
        # Restore the branch's exact starting content. A path absent at HEAD must be
        # removed from both index and worktree; git restore cannot restore a nonexistent path.
        restored = _git("restore", "--source=HEAD", "--staged", "--worktree", "--", path, check=False)
        if _git("cat-file", "-e", f"HEAD:{path}", check=False).returncode != 0:
            _git("rm", "-f", "--cached", "--ignore-unmatch", "--", path, check=False)
            target = ROOT / path
            if target.is_file() or target.is_symlink():
                target.unlink()
        return False
    return True


def apply_ancillary_patch(commit: str, path: str, extra_base: str, extra_commit: str) -> None:
    if not _apply_path_patch(f"{commit}^", commit, path):
        return
    if path in EXTRA_PATHS and not _apply_path_patch(extra_base, extra_commit, path):
        # Preserve the exact conflicting Android/document path. If it still contains
        # active provider wiring, the final scan will reject this branch instead.
        return
    target = ROOT / path
    if target.exists():
        current = target.read_text(encoding="utf-8")
        cleaned = scrub_ancillary_text(path, current)
        if cleaned != current:
            target.write_text(cleaned, encoding="utf-8")


def migrate(commit: str, extra_base: str, extra_commit: str) -> None:
    # Replace network adapters but preserve non-provider branch-specific helpers.
    transform_ai_providers(commit)
    write_text("scripts/check_ai_models.py", canonical_text(commit, "scripts/check_ai_models.py"))
    for path in ("app/native_ai.py", "scripts/check_native_ai.py"):
        target = ROOT / path
        if not target.exists():
            write_text(path, canonical_text(commit, path))
        elif PROVIDER_RE.search(target.read_text(encoding="utf-8")):
            raise MigrationError(f"{path} has a divergent provider-bearing implementation; refusing overwrite")

    transform_config()
    transform_strategy_ai(commit)
    transform_daily_research()
    transform_release_diagnostics(extra_commit)
    for path in ANCILLARY_PATHS:
        apply_ancillary_patch(commit, path, extra_base, extra_commit)

    remaining = []
    for path in NATIVE_PATHS:
        target = ROOT / path
        if target.is_file() and PROVIDER_RE.search(target.read_text(encoding="utf-8")):
            remaining.append(path)
    if remaining:
        raise MigrationError("provider references remain in: " + ", ".join(remaining))

    for path in ("app/config.py", "app/daily_research.py", "app/strategy_ai.py", "app/ai_providers.py", "app/native_ai.py", "app/main.py"):
        target = ROOT / path
        if target.is_file():
            ast.parse(target.read_text(encoding="utf-8"), filename=path)
    for path in ("deploy/cloud-run-deploy.sh", "deploy/cloud-run-worker-deploy.sh",
                 "deploy/gcp-bootstrap.sh", "scripts/verify_release.sh"):
        target = ROOT / path
        if target.is_file():
            check = subprocess.run(["bash", "-n", str(target)], cwd=ROOT, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if check.returncode:
                raise MigrationError(f"{path} fails bash -n: {check.stderr.strip()}")
    _git("add", "-A")
    _git("diff", "--cached", "--check")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--canonical-commit", default=DEFAULT_COMMIT)
    parser.add_argument("--extra-base-commit", default=DEFAULT_EXTRA_BASE_COMMIT)
    parser.add_argument("--extra-commit", required=True)
    args = parser.parse_args()
    try:
        _git("cat-file", "-e", f"{args.extra_base_commit}^{{commit}}")
        _git("cat-file", "-e", f"{args.extra_commit}^{{commit}}")
        migrate(args.canonical_commit, args.extra_base_commit, args.extra_commit)
    except (MigrationError, OSError, SyntaxError, json.JSONDecodeError) as exc:
        print(f"SAFE MIGRATION REFUSED: {exc}")
        return 1
    print("PASS: targeted Native AI migration preserved other branch files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
