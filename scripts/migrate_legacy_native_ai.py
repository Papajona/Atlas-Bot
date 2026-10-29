#!/usr/bin/env python3
"""Semantic provider-removal migration for divergent Atlas legacy branches.

This edits provider-specific functions/settings rather than replaying a broad commit
whose unrelated hunks collide with each branch. It deliberately refuses uncertain edits.
"""
from __future__ import annotations

import ast
import builtins
import symtable
import json
import re
import subprocess
from pathlib import Path

ROOT = Path.cwd()
DEFAULT_CLEANUP_COMMIT = "f5e303dad1a93ce1a33940b3e2b16d118897ebf6"
# Reviewed source for canonical Native AI behavior, distinct from the cleanup patch base.
DEFAULT_CANONICAL_COMMIT = "f8bb968198ec909e66cc04b888716f87ded6c0fa"
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
# Final checks intentionally distinguish retired providers from Groq, which remains
# supported as a separate research-only integration.
RETIRED_PROVIDER_RE = re.compile(
    r"(?i)(?:gemini|cerebras|ai[\s_-]*gateway|generativelanguage\.googleapis\.com|"
    r"api\.cerebras\.ai|api\.openai\.com|"
    r"AI_GATEWAY_[A-Z0-9_]+|CEREBRAS_[A-Z0-9_]+|OPENAI_[A-Z0-9_]+|openai-compatible|"
    r"GEMINI_API_KEY|SECRET_GEMINI|_gemini_json|GROQ_STRATEGY_MODEL|GROQ_STRATEGY_API_KEY|groq_strategy_model|"
    r"settings\.(?:gemini|cerebras|ai_gateway)_)"
)
RETIRED_PROVIDER_FIELD_RE = re.compile(
    r"(?i)(?:gemini|cerebras|ai[\s_-]*gateway|generativelanguage\.googleapis\.com|"
    r"api\.cerebras\.ai|api\.openai\.com|"
    r"AI_GATEWAY_[A-Z0-9_]+|CEREBRAS_[A-Z0-9_]+|OPENAI_[A-Z0-9_]+|openai-compatible|_gemini_json|GROQ_STRATEGY_MODEL|GROQ_STRATEGY_API_KEY|groq_strategy_model)"
)
ATLAS_PROVIDER_SECRET_RE = re.compile(
    r"(?i)\batlas[-_](?:openai|gemini|cerebras|ai[-_]gateway)[-_][a-z0-9_-]*\b"
)
PROVIDER_SETTING_NAME_RE = re.compile(
    r"(?i)^(?:SECRET_)?(?:GEMINI|CEREBRAS|AI_GATEWAY|OPENAI)_[A-Z0-9_]+$"
)
KEY_ASSIGNMENT_RE = re.compile(
    r"""(?ix)\b(?:SECRET_)?(?:GEMINI|CEREBRAS|AI_GATEWAY|OPENAI)[A-Z0-9_]*\b
    \s*[:=]\s*(?:\$\{\{[^}]+\}\}|"[^"]*"|'[^']*'|[^,\s"']+)"""
)
SECRET_VERSION_RE = re.compile(
    r"(?i)\bSECRET_(?:GEMINI|CEREBRAS|AI_GATEWAY|OPENAI)[A-Z0-9_]*\s*:\s*\$\{\{[^}]+\}\}"
)
HOST_RE = re.compile(r"""(?i)https?://(?:generativelanguage\.googleapis\.com|api\.cerebras\.ai|api\.openai\.com)[^\s"']*""")
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
    "app/groq_ai.py",
    "app/prompt_safety.py",
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
    "app/ai_providers.py", "app/groq_ai.py", "app/prompt_safety.py", "app/config.py", "app/daily_research.py",
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


def _safe_canonical_trade_safety_validator(
    node: ast.AST, canonical_node: ast.AST, source_segment: str
) -> bool:
    """Accept only the canonical local safety-payload validator contract.

    Several old provider facades carry this validator alongside hosted safety
    calls. Its typed payload checks are also present in the Native AI facade.
    Remove the duplicate only when the complete executable AST matches that
    canonical validator after ignoring inert annotations, docstrings, and
    validation error wording. Any extra check or side effect remains a refusal.
    """
    if (
        not isinstance(node, ast.FunctionDef)
        or not isinstance(canonical_node, ast.FunctionDef)
        or node.name != "_validate_trade_safety_payload"
        or canonical_node.name != node.name
        or node.decorator_list
        or canonical_node.decorator_list
        or PROVIDER_RE.search(source_segment)
    ):
        return False

    def safe_annotation(annotation: ast.AST | None) -> bool:
        if annotation is None:
            return True
        if isinstance(annotation, ast.Name):
            return annotation.id in {"Any", "dict", "str", "int", "float", "bool", "list", "tuple"}
        if isinstance(annotation, ast.Constant):
            return annotation.value is None or isinstance(annotation.value, str)
        if isinstance(annotation, ast.Subscript):
            return safe_annotation(annotation.value) and safe_annotation(annotation.slice)
        if isinstance(annotation, (ast.Tuple, ast.List)):
            return all(safe_annotation(item) for item in annotation.elts)
        return False

    def eligible_signature(function: ast.FunctionDef) -> bool:
        args = function.args
        if (
            args.posonlyargs
            or [arg.arg for arg in args.args] != ["value"]
            or args.vararg
            or args.kwarg
            or args.kwonlyargs
            or args.defaults
            or args.kw_defaults
        ):
            return False
        return (
            all(safe_annotation(arg.annotation) for arg in args.args)
            and safe_annotation(function.returns)
        )

    if not eligible_signature(node) or not eligible_signature(canonical_node):
        return False

    def inert_error_message(expression: ast.AST) -> bool:
        # The canonical validator uses literal messages and f-strings interpolating
        # the local loop variable only. Never normalize away calls, attributes, or
        # other expressions whose evaluation could have side effects or raise.
        if isinstance(expression, ast.Constant):
            return isinstance(expression.value, str)
        if not isinstance(expression, ast.JoinedStr):
            return False
        for part in expression.values:
            if isinstance(part, ast.Constant):
                if not isinstance(part.value, str):
                    return False
                continue
            if not (
                isinstance(part, ast.FormattedValue)
                and isinstance(part.value, ast.Name)
                and part.value.id == "field"
                and part.conversion == -1
                and part.format_spec is None
            ):
                return False
        return True

    def normalized(function: ast.FunctionDef) -> str | None:
        try:
            clone = ast.parse(ast.unparse(function)).body[0]
        except (SyntaxError, ValueError):
            return None
        if not isinstance(clone, ast.FunctionDef):
            return None
        clone.args.args[0].annotation = None
        clone.returns = None
        if (
            clone.body
            and isinstance(clone.body[0], ast.Expr)
            and isinstance(clone.body[0].value, ast.Constant)
            and isinstance(clone.body[0].value.value, str)
        ):
            clone.body.pop(0)
        for part in ast.walk(clone):
            if not isinstance(part, ast.Raise) or part.exc is None:
                continue
            error = part.exc
            if (
                not isinstance(error, ast.Call)
                or not isinstance(error.func, ast.Name)
                or error.func.id != "AIProviderError"
                or len(error.args) != 1
                or error.keywords
                or not inert_error_message(error.args[0])
            ):
                return None
            error.args[0] = ast.Constant(value="__CANONICAL_VALIDATION_ERROR__")
        ast.fix_missing_locations(clone)
        return ast.dump(clone, include_attributes=False)

    legacy_contract = normalized(node)
    canonical_contract = normalized(canonical_node)
    return legacy_contract is not None and legacy_contract == canonical_contract


def _safe_canonical_json_helper(
    node: ast.AST, name: str, source_segment: str, legacy_tree: ast.Module
) -> bool:
    """Whitelist the known pure legacy JSON helper shapes; reject semantic variants."""
    if (
        name not in {"_extract_json", "_response_json_object"}
        or not isinstance(node, ast.FunctionDef)
        or node.decorator_list
        or PROVIDER_RE.search(source_segment)
    ):
        return False

    args = node.args
    if args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg or args.kw_defaults:
        return False

    # Annotations execute at function definition unless postponed. Allow only inert,
    # well-known type names and simple generic subscriptions; reject calls/attribute
    # lookups and other expressions whose side effects would disappear on replacement.
    safe_annotation_names = {"str", "dict", "Any", "int", "float", "bool", "list", "tuple"}

    def safe_annotation(annotation: ast.AST | None) -> bool:
        if annotation is None:
            return True
        if isinstance(annotation, ast.Name):
            return annotation.id in safe_annotation_names
        if isinstance(annotation, ast.Constant):
            return annotation.value is None or isinstance(annotation.value, str)
        if isinstance(annotation, ast.Subscript):
            return safe_annotation(annotation.value) and safe_annotation(annotation.slice)
        if isinstance(annotation, (ast.Tuple, ast.List)):
            return all(safe_annotation(element) for element in annotation.elts)
        return False

    if any(not safe_annotation(argument.annotation) for argument in args.args):
        return False
    if not safe_annotation(node.returns):
        return False

    if name == "_extract_json":
        if [arg.arg for arg in args.args] != ["text"] or args.defaults:
            return False
        return_name = "value"
        json_import_count = 0

        def has_json_named_expression(nodes: list[ast.AST]) -> bool:
            """Detect walrus assignments that leak into the enclosing module scope."""
            pending = list(nodes)
            while pending:
                current = pending.pop()
                if (
                    isinstance(current, ast.NamedExpr)
                    and isinstance(current.target, ast.Name)
                    and current.target.id == "json"
                ):
                    return True
                if isinstance(current, ast.Lambda):
                    # Lambda bodies are deferred, but their default expressions execute now.
                    pending.extend(current.args.defaults)
                    pending.extend(default for default in current.args.kw_defaults if default is not None)
                    continue
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                pending.extend(ast.iter_child_nodes(current))
            return False

        def class_body_writes_global_json(node: ast.ClassDef) -> bool:
            """Detect class-body writes explicitly redirected to the module binding."""
            pending = list(reversed(node.body))
            declares_global = False
            writes_global = False
            while pending:
                current = pending.pop()
                if isinstance(current, ast.Global) and "json" in current.names:
                    declares_global = True
                if (
                    isinstance(current, ast.Name)
                    and current.id == "json"
                    and isinstance(current.ctx, (ast.Store, ast.Del))
                ):
                    writes_global = True
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    continue
                if isinstance(current, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                    if has_json_named_expression([current]):
                        return True
                    continue
                pending.extend(reversed(list(ast.iter_child_nodes(current))))
            return declares_global and writes_global

        def module_scope_walk(statements: list[ast.stmt]):
            """Walk module-executed statements without entering nested lexical scopes."""
            pending = list(reversed(statements))
            while pending:
                current = pending.pop()
                yield current
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                if isinstance(current, ast.Lambda):
                    # Lambda bodies are deferred, but defaults execute in module scope.
                    pending.extend(reversed(current.args.defaults))
                    pending.extend(reversed([default for default in current.args.kw_defaults if default is not None]))
                    continue
                if isinstance(current, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                    continue
                pending.extend(reversed(list(ast.iter_child_nodes(current))))

        # Require at least one unconditional standard-library import. Repeating
        # that import is harmless and should not prevent migration.
        for statement in legacy_tree.body:
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    bound = alias.asname or alias.name.split(".", 1)[0]
                    if bound == "json":
                        if alias.name != "json" or alias.asname is not None:
                            return False
                        json_import_count += 1
        # Refuse bindings that execute in module scope, including bindings under
        # control flow, class bodies that write through global, and walrus assignments
        # that escape comprehensions. Ignore ordinary function/class locals.
        for nested in module_scope_walk(legacy_tree.body):
            if isinstance(nested, ast.Import):
                for alias in nested.names:
                    bound = alias.asname or alias.name.split(".", 1)[0]
                    if bound == "json" and (alias.name != "json" or alias.asname is not None):
                        return False
            elif isinstance(nested, ast.ImportFrom):
                if any((alias.asname or alias.name) == "json" for alias in nested.names):
                    return False
            if isinstance(nested, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if nested.name == "json":
                    return False
                if isinstance(nested, ast.ClassDef):
                    if class_body_writes_global_json(nested):
                        return False
                    executed_expressions = [
                        *nested.decorator_list, *nested.bases,
                        *(keyword.value for keyword in nested.keywords),
                    ]
                else:
                    executed_expressions = [
                        *nested.decorator_list, *nested.args.defaults,
                        *(default for default in nested.args.kw_defaults if default is not None),
                        *(argument.annotation for argument in [*nested.args.posonlyargs, *nested.args.args, *nested.args.kwonlyargs] if argument.annotation is not None),
                        *([nested.args.vararg.annotation] if nested.args.vararg and nested.args.vararg.annotation is not None else []),
                        *([nested.args.kwarg.annotation] if nested.args.kwarg and nested.args.kwarg.annotation is not None else []),
                        *([nested.returns] if nested.returns is not None else []),
                    ]
                if has_json_named_expression(executed_expressions):
                    return False
            if isinstance(nested, ast.Lambda) and has_json_named_expression(
                [*nested.args.defaults, *(default for default in nested.args.kw_defaults if default is not None)]
            ):
                return False
            if isinstance(nested, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                if has_json_named_expression([nested]):
                    return False
            if isinstance(nested, ast.Name) and nested.id == "json" and isinstance(nested.ctx, (ast.Store, ast.Del)):
                return False
            if isinstance(nested, ast.ExceptHandler) and nested.name == "json":
                return False
        if json_import_count < 1:
            return False
        if json_import_count < 1:
            return False
        allowed_exceptions = {
            "json.JSONDecodeError", "TypeError", "ValueError", "UnicodeDecodeError",
        }
    else:
        if [arg.arg for arg in args.args] != ["response", "provider"]:
            return False
        if len(args.defaults) not in {0, 1}:
            return False
        if args.defaults and not (
            isinstance(args.defaults[0], ast.Constant) and isinstance(args.defaults[0].value, str)
        ):
            return False
        return_name = "body"
        allowed_exceptions = {"ValueError", "UnicodeDecodeError", "TypeError", "AttributeError"}

    def is_name(value: ast.AST, expected: str) -> bool:
        return isinstance(value, ast.Name) and value.id == expected

    def is_string(value: ast.AST, expected: str) -> bool:
        return isinstance(value, ast.Constant) and value.value == expected

    def is_error_raise(statement: ast.stmt) -> bool:
        if not isinstance(statement, ast.Raise) or not isinstance(statement.exc, ast.Call):
            return False
        call = statement.exc
        if not is_name(call.func, "AIProviderError") or not call.args or call.keywords:
            return False
        for argument in call.args:
            for part in ast.walk(argument):
                if isinstance(part, (ast.Call, ast.Attribute, ast.Subscript, ast.NamedExpr, ast.Lambda)):
                    return False
                if isinstance(part, ast.FormattedValue) and not (
                    isinstance(part.value, ast.Name) and part.format_spec is None
                ):
                    return False
                if not isinstance(part, (ast.Constant, ast.JoinedStr, ast.Name, ast.BinOp, ast.FormattedValue)):
                    if isinstance(part, ast.expr):
                        return False
        return True

    def is_isinstance_call(value: ast.AST, left: str, right: str) -> bool:
        return (
            isinstance(value, ast.Call)
            and is_name(value.func, "isinstance")
            and len(value.args) == 2
            and is_name(value.args[0], left)
            and is_name(value.args[1], right)
            and not value.keywords
        )

    def is_strip_call(value: ast.AST, receiver: str | None = None) -> bool:
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute) or value.func.attr != "strip" or value.keywords:
            return False
        if receiver is not None and not is_name(value.func.value, receiver):
            return False
        if is_name(value.func.value, "text") or is_name(value.func.value, "raw"):
            return not value.args
        if (
            isinstance(value.func.value, ast.BoolOp) and isinstance(value.func.value.op, ast.Or)
            and len(value.func.value.values) == 2
            and is_name(value.func.value.values[0], "text")
            and is_string(value.func.value.values[1], "")
        ):
            return not value.args
        if (
            is_name(value.func.value, "raw")
            and len(value.args) == 1
            and is_string(value.args[0], "\x60")
        ):
            return True
        return False

    def is_safe_empty_text_condition(condition: ast.AST) -> bool:
        if isinstance(condition, ast.UnaryOp) and isinstance(condition.op, ast.Not):
            return is_strip_call(condition.operand, "text")
        if isinstance(condition, ast.BoolOp) and isinstance(condition.op, ast.Or) and len(condition.values) == 2:
            first, second = condition.values
            return (
                isinstance(first, ast.UnaryOp) and isinstance(first.op, ast.Not)
                and is_isinstance_call(first.operand, "text", "str")
                and isinstance(second, ast.UnaryOp) and isinstance(second.op, ast.Not)
                and is_safe_empty_text_condition(second)
            )
        return False

    def is_safe_condition(condition: ast.AST) -> str | None:
        # Only canonical rejection checks are eligible. Inverted or additional conditions
        # can change which valid JSON values callers accept, so reject them.
        if isinstance(condition, ast.UnaryOp) and isinstance(condition.op, ast.Not):
            operand = condition.operand
            if name == "_extract_json" and is_isinstance_call(operand, "text", "str"):
                return "raise"
            expected_value = "value" if name == "_extract_json" else "body"
            if is_isinstance_call(operand, expected_value, "dict"):
                return "object-guard"
            if name == "_extract_json" and is_strip_call(operand, "text"):
                return "raise"
        if name == "_extract_json" and is_safe_empty_text_condition(condition):
            return "raise"
        if (
            name == "_extract_json"
            and isinstance(condition, ast.Call)
            and isinstance(condition.func, ast.Attribute)
            and condition.func.attr == "startswith"
            and is_name(condition.func.value, "raw")
            and len(condition.args) == 1
            and not condition.keywords
        ):
            if is_string(condition.args[0], "\x60\x60\x60"):
                return "fence"
            if is_string(condition.args[0], "json"):
                return "json-prefix"
        return None

    def safe_raw_assignment(value: ast.AST) -> bool:
        if is_name(value, "text"):
            return True
        if (
            isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or) and len(value.values) == 2
            and is_name(value.values[0], "text") and is_string(value.values[1], "")
        ):
            return True
        if is_strip_call(value):
            return True
        if (
            isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
            and value.func.attr == "strip" and is_name(value.func.value, "raw")
            and len(value.args) == 1 and is_string(value.args[0], "\x60") and not value.keywords
        ):
            return True
        if (
            isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
            and value.func.attr == "lstrip" and isinstance(value.func.value, ast.Subscript)
            and is_name(value.func.value.value, "raw")
            and isinstance(value.func.value.slice, ast.Slice)
            and is_string(value.func.value.slice.lower, "") is False
            and isinstance(value.func.value.slice.lower, ast.Constant)
            and value.func.value.slice.lower.value == 4
            and value.func.value.slice.upper is None and value.func.value.slice.step is None
            and not value.args and not value.keywords
        ):
            return True
        return False

    def safe_parser_assignment(statement: ast.Assign) -> bool:
        if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
            return False
        target = statement.targets[0].id
        value = statement.value
        if name == "_extract_json" and target == "value":
            return (
                isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                and value.func.attr == "loads" and is_name(value.func.value, "json")
                and len(value.args) == 1 and isinstance(value.args[0], ast.Name)
                and value.args[0].id in {"raw", "text"} and not value.keywords
            )
        if name == "_response_json_object" and target == "body":
            return (
                isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                and value.func.attr == "json" and is_name(value.func.value, "response")
                and not value.args and not value.keywords
            )
        return False

    def safe_try(statement: ast.Try) -> bool:
        if statement.orelse or statement.finalbody or len(statement.body) != 1 or not isinstance(statement.body[0], ast.Assign):
            return False
        if not safe_parser_assignment(statement.body[0]) or not statement.handlers:
            return False
        for handler in statement.handlers:
            if handler.type is None or len(handler.body) != 1 or not is_error_raise(handler.body[0]):
                return False
            exception_nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
            exception_names = {ast.unparse(item) for item in exception_nodes}
            if not exception_names or not exception_names.issubset(allowed_exceptions):
                return False
        return True

    def safe_fence_if(statement: ast.If) -> bool:
        if statement.orelse or not statement.body or len(statement.body) > 2:
            return False
        first = statement.body[0]
        if not isinstance(first, ast.Assign) or len(first.targets) != 1 or not is_name(first.targets[0], "raw"):
            return False
        first_value = first.value
        if not (
            isinstance(first_value, ast.Call) and isinstance(first_value.func, ast.Attribute)
            and first_value.func.attr == "strip" and is_name(first_value.func.value, "raw")
            and len(first_value.args) == 1 and is_string(first_value.args[0], "\x60")
            and not first_value.keywords
        ):
            return False
        if len(statement.body) == 1:
            return True
        second = statement.body[1]
        if not isinstance(second, ast.If) or is_safe_condition(second.test) != "json-prefix" or second.orelse:
            return False
        if len(second.body) != 1 or not isinstance(second.body[0], ast.Assign):
            return False
        assigned = second.body[0]
        if len(assigned.targets) != 1 or not is_name(assigned.targets[0], "raw"):
            return False
        expression = assigned.value
        return (
            isinstance(expression, ast.Call) and isinstance(expression.func, ast.Attribute)
            and expression.func.attr == "lstrip" and isinstance(expression.func.value, ast.Subscript)
            and is_name(expression.func.value.value, "raw")
            and isinstance(expression.func.value.slice, ast.Slice)
            and isinstance(expression.func.value.slice.lower, ast.Constant)
            and expression.func.value.slice.lower.value == 4
            and expression.func.value.slice.upper is None and expression.func.value.slice.step is None
            and not expression.args and not expression.keywords
        )

    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    if not body or not isinstance(body[-1], ast.Return) or not is_name(body[-1].value, return_name):
        return False

    saw_parse = False
    saw_error = False
    saw_object_guard = False
    for statement in body:
        if isinstance(statement, ast.Assign):
            if name == "_extract_json" and len(statement.targets) == 1 and is_name(statement.targets[0], "raw"):
                if not safe_raw_assignment(statement.value):
                    return False
            elif safe_parser_assignment(statement):
                saw_parse = True
            else:
                return False
        elif isinstance(statement, ast.Try):
            if not safe_try(statement):
                return False
            saw_parse = True
            saw_error = True
        elif isinstance(statement, ast.If):
            condition_kind = is_safe_condition(statement.test)
            if condition_kind == "fence":
                if name != "_extract_json" or not safe_fence_if(statement):
                    return False
            elif condition_kind == "json-prefix":
                # The JSON-prefix cleanup is allowed only as the nested second statement
                # inside the recognized fenced-JSON branch.
                return False
            elif condition_kind in {"raise", "object-guard"}:
                if statement.orelse or len(statement.body) != 1 or not is_error_raise(statement.body[0]):
                    return False
                saw_error = True
                if condition_kind == "object-guard":
                    saw_object_guard = True
            else:
                return False
        elif isinstance(statement, ast.Return):
            if not is_name(statement.value, return_name):
                return False
        else:
            return False

    # Every nested expression must belong to the whitelisted statement forms. This also
    # prevents hidden calls, mutable writes, alternate return paths and custom parser checks.
    allowed_call_names = {"AIProviderError", "isinstance"}
    allowed_load_names = (
        {"text", "raw", "value", "exc", "json", "AIProviderError", "isinstance",
         "str", "dict", "TypeError", "ValueError", "UnicodeDecodeError"}
        if name == "_extract_json" else
        {"response", "provider", "body", "exc", "AIProviderError", "isinstance",
         "dict", "ValueError", "UnicodeDecodeError", "TypeError", "AttributeError"}
    )
    for item in (child for statement in node.body for child in ast.walk(statement)):
        if isinstance(item, ast.Call):
            if isinstance(item.func, ast.Name) and item.func.id in allowed_call_names:
                continue
            if not isinstance(item.func, ast.Attribute):
                return False
            if name == "_extract_json":
                if item.func.attr == "loads" and is_name(item.func.value, "json"):
                    if (
                        len(item.args) == 1 and isinstance(item.args[0], ast.Name)
                        and item.args[0].id in {"raw", "text"} and not item.keywords
                    ):
                        continue
                    return False
                if item.func.attr == "strip":
                    if not item.keywords and (
                        not item.args
                        or (
                            is_name(item.func.value, "raw") and len(item.args) == 1
                            and is_string(item.args[0], "\x60")
                        )
                    ) and (
                        is_name(item.func.value, "text") or is_name(item.func.value, "raw")
                        or (
                            isinstance(item.func.value, ast.BoolOp) and isinstance(item.func.value.op, ast.Or)
                            and len(item.func.value.values) == 2
                            and is_name(item.func.value.values[0], "text")
                            and is_string(item.func.value.values[1], "")
                        )
                    ):
                        continue
                if item.func.attr == "startswith":
                    if (
                        is_name(item.func.value, "raw") and len(item.args) == 1
                        and is_string(item.args[0], "\x60\x60\x60") or
                        is_name(item.func.value, "raw") and len(item.args) == 1
                        and is_string(item.args[0], "json")
                    ) and not item.keywords:
                        continue
                if item.func.attr == "lstrip":
                    receiver=item.func.value
                    if (
                        isinstance(receiver, ast.Subscript) and is_name(receiver.value, "raw")
                        and isinstance(receiver.slice, ast.Slice)
                        and isinstance(receiver.slice.lower, ast.Constant) and receiver.slice.lower.value == 4
                        and receiver.slice.upper is None and receiver.slice.step is None
                        and not item.args and not item.keywords
                    ):
                        continue
            elif item.func.attr == "json" and is_name(item.func.value, "response"):
                if not item.args and not item.keywords:
                    continue
            return False
        if isinstance(item, ast.Name) and item.id == "json" and isinstance(item.ctx, (ast.Store, ast.Del)):
            return False
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load) and item.id not in allowed_load_names:
            return False
        if isinstance(item, ast.Attribute):
            if name == "_extract_json" and is_name(item.value, "json") and item.attr in {"loads", "JSONDecodeError"}:
                continue
            if name == "_response_json_object" and is_name(item.value, "response") and item.attr == "json":
                continue
            if name == "_extract_json" and item.attr in {"strip", "startswith", "lstrip"}:
                receiver = item.value
                if is_name(receiver, "text") or is_name(receiver, "raw"):
                    continue
                if (
                    isinstance(receiver, ast.BoolOp) and isinstance(receiver.op, ast.Or)
                    and len(receiver.values) == 2 and is_name(receiver.values[0], "text")
                    and is_string(receiver.values[1], "")
                ):
                    continue
                if (
                    isinstance(receiver, ast.Subscript) and is_name(receiver.value, "raw")
                    and isinstance(receiver.slice, ast.Slice)
                    and isinstance(receiver.slice.lower, ast.Constant)
                    and receiver.slice.lower.value == 4 and receiver.slice.upper is None
                    and receiver.slice.step is None
                ):
                    continue
            return False
        if isinstance(item, (ast.Global, ast.Nonlocal, ast.Await, ast.Yield, ast.YieldFrom,
                             ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            return False

    return saw_parse and saw_error and saw_object_guard


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
        # Only the standalone review settings survive. The old Groq strategy selector
        # is retired with the hosted strategy generator.
        supported_groq_fields = {
            "groq_api_key", "groq_model", "groq_max_completion_tokens",
            "groq_timeout_seconds", "groq_research_review_enabled",
        }
        return bool(PROVIDER_FIELD_RE.search(name)) and name.lower() not in supported_groq_fields

    postponed_annotations = _uses_postponed_annotations(tree)

    def module_binding_before(name: str, line: int) -> ast.stmt | None:
        """Return the last top-level statement that can bind name before line."""
        last: ast.stmt | None = None
        for statement in tree.body:
            if statement.lineno >= line:
                break
            bindings = _defined_names(statement) | _module_scope_maybe_binds(
                statement, postponed_annotations=postponed_annotations
            )
            if (
                name in bindings
                or (
                    isinstance(statement, ast.ImportFrom)
                    and any(alias.name == "*" for alias in statement.names)
                )
            ):
                # A star import may overwrite any name; only a later explicit,
                # reviewed binding can re-establish a trusted decorator import.
                last = statement
        return last

    def class_statement_binds_name(statement: ast.stmt, name: str) -> bool:
        """Conservatively detect a class-suite rebinding, but not method-local stores."""
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return statement.name == name
        if name in _defined_names(statement):
            return True
        if isinstance(statement, ast.ImportFrom) and any(
            alias.name == "*" for alias in statement.names
        ):
            return True
        stack = list(ast.iter_child_nodes(statement))
        while stack:
            item = stack.pop()
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                # Nested scopes do not bind the surrounding class namespace.
                continue
            if isinstance(item, ast.Name) and item.id == name and isinstance(item.ctx, (ast.Store, ast.Del)):
                return True
            if isinstance(item, ast.Global) and name in item.names:
                return True
            if isinstance(item, ast.Nonlocal) and name in item.names:
                return True
            if isinstance(item, (ast.Import, ast.ImportFrom)) and (
                name in _defined_names(item)
                or (
                    isinstance(item, ast.ImportFrom)
                    and any(alias.name == "*" for alias in item.names)
                )
            ):
                return True
            stack.extend(ast.iter_child_nodes(item))
        return False

    def decorators_resolve_to_reviewed_implementations(
        settings_class: ast.ClassDef, validator: ast.FunctionDef
    ) -> bool:
        # A bare decorator identifier is executable at class-definition time.
        # Require the final module binding to be the explicit Pydantic import and
        # prohibit class-local shadows before this validator is defined.
        field_import = module_binding_before("field_validator", settings_class.lineno)
        if not (
            isinstance(field_import, ast.ImportFrom)
            and field_import.module == "pydantic"
            and any(
                alias.name == "field_validator"
                and (alias.asname or alias.name) == "field_validator"
                for alias in field_import.names
            )
        ):
            return False
        if module_binding_before("classmethod", settings_class.lineno) is not None:
            return False
        for statement in settings_class.body:
            if statement is validator:
                break
            if (
                class_statement_binds_name(statement, "field_validator")
                or class_statement_binds_name(statement, "classmethod")
            ):
                return False
        return True

    def is_safe_gemini_model_validator(node: ast.AST) -> bool:
        """Recognize only the known pure validator for a setting being removed."""
        if not isinstance(node, ast.FunctionDef) or node.name != "_validate_gemini_model":
            return False
        args = node.args
        if (
            args.posonlyargs
            or [argument.arg for argument in args.args] != ["cls", "value"]
            or args.vararg
            or args.kwarg
            or args.kwonlyargs
            or args.defaults
            or args.kw_defaults
        ):
            return False

        # Annotation expressions run when the Settings class is created. Retire
        # this validator only when its annotations are inert type names.
        def safe_annotation(annotation: ast.AST | None) -> bool:
            return annotation is None or (
                isinstance(annotation, ast.Name) and annotation.id == "str"
            )

        if (
            any(not safe_annotation(argument.annotation) for argument in args.args)
            or not safe_annotation(node.returns)
        ):
            return False
        if len(node.decorator_list) != 2:
            return False
        field_decorators = [
            decorator for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "field_validator"
        ]
        classmethod_decorators = [
            decorator for decorator in node.decorator_list
            if isinstance(decorator, ast.Name) and decorator.id == "classmethod"
        ]
        if len(field_decorators) != 1 or len(classmethod_decorators) != 1:
            return False
        field_decorator = field_decorators[0]
        if (
            field_decorator.keywords
            or not field_decorator.args
            or any(
                not isinstance(argument, ast.Constant)
                or not isinstance(argument.value, str)
                or not is_provider_field(argument.value)
                for argument in field_decorator.args
            )
        ):
            return False

        body = list(node.body)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        if len(body) != 3:
            return False

        assignment, condition, returned = body
        if not isinstance(assignment, ast.Assign) or (
            len(assignment.targets) != 1
            or not isinstance(assignment.targets[0], ast.Name)
            or assignment.targets[0].id != "normalized"
        ):
            return False
        outer = assignment.value
        if not (
            isinstance(outer, ast.Call)
            and not outer.keywords
            and len(outer.args) == 1
            and isinstance(outer.args[0], ast.Constant)
            and outer.args[0].value == "models/"
            and isinstance(outer.func, ast.Attribute)
            and outer.func.attr == "removeprefix"
        ):
            return False
        stripped = outer.func.value
        if not (
            isinstance(stripped, ast.Call)
            and not stripped.args
            and not stripped.keywords
            and isinstance(stripped.func, ast.Attribute)
            and stripped.func.attr == "strip"
        ):
            return False
        stringified = stripped.func.value
        if not (
            isinstance(stringified, ast.Call)
            and isinstance(stringified.func, ast.Name)
            and stringified.func.id == "str"
            and len(stringified.args) == 1
            and not stringified.keywords
            and isinstance(stringified.args[0], ast.BoolOp)
            and isinstance(stringified.args[0].op, ast.Or)
            and len(stringified.args[0].values) == 2
            and isinstance(stringified.args[0].values[0], ast.Name)
            and stringified.args[0].values[0].id == "value"
            and isinstance(stringified.args[0].values[1], ast.Constant)
            and stringified.args[0].values[1].value == ""
        ):
            return False
        if not (
            isinstance(condition, ast.If)
            and not condition.orelse
            and isinstance(condition.test, ast.Compare)
            and isinstance(condition.test.left, ast.Name)
            and condition.test.left.id == "normalized"
            and len(condition.test.ops) == 1
            and isinstance(condition.test.ops[0], ast.NotEq)
            and len(condition.test.comparators) == 1
            and isinstance(condition.test.comparators[0], ast.Constant)
            and isinstance(condition.test.comparators[0].value, str)
            and re.fullmatch(r"gemini-[A-Za-z0-9_.-]+", condition.test.comparators[0].value)
            and len(condition.body) == 1
            and isinstance(condition.body[0], ast.Raise)
            and isinstance(condition.body[0].exc, ast.Call)
            and isinstance(condition.body[0].exc.func, ast.Name)
            and condition.body[0].exc.func.id == "ValueError"
            and len(condition.body[0].exc.args) == 1
            and not condition.body[0].exc.keywords
            and isinstance(condition.body[0].exc.args[0], ast.Constant)
            and condition.body[0].exc.args[0].value
                == f"Gemini model must be exactly {condition.test.comparators[0].value}; alternative Gemini IDs are not permitted."
        ):
            return False
        return (
            isinstance(returned, ast.Return)
            and isinstance(returned.value, ast.Name)
            and returned.value.id == "normalized"
        )

    # A removed provider model field may have one exact, pure field validator.
    # Remove only the recognized normalization/constant-ID check; any custom branch
    # behavior, side effect, or different validator shape remains a hard refusal.
    for settings_class in settings_classes:
        for member in settings_class.body:
            if (
                isinstance(member, ast.FunctionDef)
                and is_safe_gemini_model_validator(member)
                and decorators_resolve_to_reviewed_implementations(settings_class, member)
            ):
                first_line = min(
                    [member.lineno, *[decorator.lineno for decorator in member.decorator_list]]
                )
                removable_lines.update(range(first_line, (member.end_lineno or member.lineno) + 1))

    for node in assignments:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = {name for target in targets for name in target_names(target)}
        supported_groq_fields = {
            "groq_api_key", "groq_model", "groq_max_completion_tokens",
            "groq_timeout_seconds", "groq_research_review_enabled",
        }
        supported_groq_names = {
            name for name in names
            if name.lower() in supported_groq_fields and PROVIDER_FIELD_RE.search(name)
        }
        provider_names = {name for name in names if is_provider_field(name)}
        if supported_groq_names and id(node) not in direct_setting_assignment_ids:
            raise MigrationError(
                "app/config.py contains a supported Groq setting outside the Settings class; refusing helper behavior"
            )
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
        if not (line.lstrip().startswith("#") and RETIRED_PROVIDER_RE.search(line))
    )
    # Supply the separate Groq research-review settings on legacy branches that
    # predate them, without overwriting settings already present on a branch.
    post_cleanup_tree = ast.parse(result, filename=str(path))
    settings_class = next(
        (node for node in post_cleanup_tree.body
         if isinstance(node, ast.ClassDef)
         and node.name.lower() in {"settings", "appsettings", "atlassettings"}),
        None,
    )
    if settings_class is None:
        raise MigrationError("app/config.py has no recognized Settings class; cannot retain Groq")
    existing_groq_fields = {
        name
        for node in settings_class.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        for name in target_names(target)
    }
    groq_field_definitions = [
        ("groq_api_key", 'groq_api_key: str = ""'),
        ("groq_model", 'groq_model: str = "openai/gpt-oss-20b"'),
        ("groq_max_completion_tokens", "groq_max_completion_tokens: int = 2048"),
        ("groq_timeout_seconds", "groq_timeout_seconds: float = 20.0"),
        ("groq_research_review_enabled", "groq_research_review_enabled: bool = True"),
    ]
    missing_groq_fields = [
        source_line for field_name, source_line in groq_field_definitions
        if field_name not in existing_groq_fields
    ]
    if missing_groq_fields:
        class_methods = [
            node for node in settings_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        result_lines = result.splitlines(keepends=True)
        class_line = result_lines[settings_class.lineno - 1]
        class_indent = re.match(r"^[ \t]*", class_line).group(0)
        body_indent = next(
            (
                re.match(r"^[ \t]*", result_lines[member.lineno - 1]).group(0)
                for member in settings_class.body
                if len(re.match(r"^[ \t]*", result_lines[member.lineno - 1]).group(0))
                > len(class_indent)
            ),
            class_indent + ("\t" if "\t" in class_indent else "    "),
        )
        if class_methods:
            first_method_line = min(
                min([node.lineno, *[decorator.lineno for decorator in node.decorator_list]])
                for node in class_methods
            )
            insertion_index = first_method_line - 1
        else:
            # Insert after the full final statement, not inside a multiline field.
            insertion_index = settings_class.end_lineno or settings_class.lineno
        result_lines[insertion_index:insertion_index] = [
            body_indent + source_line + "\n" for source_line in missing_groq_fields
        ]
        result = "".join(result_lines)
    if RETIRED_PROVIDER_RE.search(result):
        raise MigrationError(
            "app/config.py still contains retired-provider text outside isolated Settings fields; refusing lossy cleanup"
        )
    ast.parse(result, filename=str(path))
    path.write_text(result, encoding="utf-8")


def _defined_names(node: ast.AST) -> set[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.NamedExpr):
        return _assigned_names(node.value.target)
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








def _uses_postponed_annotations(tree: ast.Module) -> bool:
    """Return whether the module defers annotation evaluation via __future__."""
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _class_scope_global_mutations(
    node: ast.ClassDef, *, postponed_annotations: bool = False
) -> set[str]:
    """Find module writes executed by a class suite through explicit global declarations."""
    declared: set[str] = set()

    def collect_declarations(part: ast.AST) -> None:
        if isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # These are separate scopes; their own global declarations do not
            # make names global in this surrounding class suite.
            return
        if isinstance(part, ast.ClassDef) and part is not node:
            return
        if isinstance(part, ast.Global):
            declared.update(part.names)
            return
        for child in ast.iter_child_nodes(part):
            collect_declarations(child)

    for statement in node.body:
        collect_declarations(statement)
    mutations: set[str] = set()

    def visit_definition_time_expressions(part: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Decorators and defaults always execute when the class body defines a
        # function. With postponed annotations, annotations are stringized and
        # must not be treated as module mutations.
        expressions: list[ast.AST] = [
            *part.decorator_list,
            *part.args.defaults,
            *(value for value in part.args.kw_defaults if value is not None),
        ]
        if not postponed_annotations:
            expressions.extend(
                arg.annotation
                for arg in [
                    *part.args.posonlyargs,
                    *part.args.args,
                    *part.args.kwonlyargs,
                ]
                if arg.annotation is not None
            )
            if part.args.vararg and part.args.vararg.annotation is not None:
                expressions.append(part.args.vararg.annotation)
            if part.args.kwarg and part.args.kwarg.annotation is not None:
                expressions.append(part.args.kwarg.annotation)
            if part.returns is not None:
                expressions.append(part.returns)
        for expression in expressions:
            visit(expression)

    def visit(part: ast.AST) -> None:
        if isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Defining a callable binds its name in the enclosing scope, and the
            # decorators/defaults/annotations execute immediately in class scope.
            if part.name in declared:
                mutations.add(part.name)
            visit_definition_time_expressions(part)
            return
        if isinstance(part, ast.Lambda):
            # Lambda bodies run later, but defaults execute when the lambda is made.
            for expression in [
                *part.args.defaults,
                *(value for value in part.args.kw_defaults if value is not None),
            ]:
                visit(expression)
            return
        if isinstance(part, ast.ClassDef) and part is not node:
            # A nested class statement binds its name in the enclosing class
            # namespace, or in the module if that name was declared global.
            if part.name in declared:
                mutations.add(part.name)
            for expression in [
                *part.decorator_list,
                *part.bases,
                *(keyword.value for keyword in part.keywords),
            ]:
                visit(expression)
            mutations.update(_class_scope_global_mutations(part, postponed_annotations=postponed_annotations))
            return
        if isinstance(part, ast.ExceptHandler):
            # The exception target is string-valued in the AST and is implicitly
            # deleted at handler exit; with global, this clears the module binding.
            if part.name and part.name in declared:
                mutations.add(part.name)
            if part.type is not None:
                visit(part.type)
            for statement in part.body:
                visit(statement)
            return
        if isinstance(part, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            # Comprehension targets are local to the implicit comprehension scope.
            # Inspect iterables, filters, and result expressions so walrus targets
            # in those expressions still count as enclosing-scope mutations.
            for generator in part.generators:
                visit(generator.iter)
                for condition in generator.ifs:
                    visit(condition)
            if isinstance(part, ast.DictComp):
                visit(part.key)
                visit(part.value)
            else:
                visit(part.elt)
            return
        if isinstance(part, ast.If):
            visit(part.test)
            condition_known = False
            try:
                condition = ast.literal_eval(part.test)
                condition_known = True
            except (ValueError, TypeError):
                condition = None
            selected = (part.body if bool(condition) else part.orelse) if condition_known else None
            statements = selected if selected is not None else [*part.body, *part.orelse]
            for statement in statements:
                visit(statement)
            return
        if isinstance(part, (ast.For, ast.AsyncFor)):
            visit(part.iter)
            try:
                iterable = ast.literal_eval(part.iter)
            except (ValueError, TypeError):
                iterable = None
            if isinstance(iterable, (list, tuple, dict, set, str, bytes)) and not iterable:
                for statement in part.orelse:
                    visit(statement)
                return
            visit(part.target)
            for statement in [*part.body, *part.orelse]:
                visit(statement)
            return
        if isinstance(part, ast.While):
            visit(part.test)
            condition_known = False
            try:
                condition = ast.literal_eval(part.test)
                condition_known = True
            except (ValueError, TypeError):
                condition = None
            statements = part.orelse if condition_known and not bool(condition) else [*part.body, *part.orelse]
            for statement in statements:
                visit(statement)
            return
        if isinstance(part, ast.AnnAssign) and postponed_annotations:
            # Postponed class/module annotations are strings, not executed AST.
            # Still inspect a real value expression for annotated assignments.
            if part.value is not None:
                visit(part.target)
                visit(part.value)
            elif not isinstance(part.target, ast.Name):
                visit(part.target)
            return
        if isinstance(part, ast.Name) and part.id in declared and isinstance(part.ctx, (ast.Store, ast.Del)):
            mutations.add(part.id)
        elif isinstance(part, (ast.Import, ast.ImportFrom)):
            mutations.update(_defined_names(part) & declared)
        for child in ast.iter_child_nodes(part):
            visit(child)

    for statement in node.body:
        visit(statement)
    return mutations

def _module_scope_maybe_binds(node: ast.AST, *, postponed_annotations: bool = False) -> set[str]:
    """Return module names a statement may bind/delete without counting local scopes.

    This is deliberately conservative for module-level control flow: a loop
    target may rebind a canonical import if an iteration occurs, but must not
    count as a guaranteed dependency in a migration.
    """
    names: set[str] = set()

    def visit(part: ast.AST) -> None:
        if isinstance(part, ast.Lambda):
            for expression in [
                *part.args.defaults,
                *(value for value in part.args.kw_defaults if value is not None),
            ]:
                visit(expression)
            return
        if isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(part.name)
            # Definition-time expressions execute in module scope; the function
            # body does not, so a local walrus/assignment must not look global.
            expressions = [
                *part.decorator_list,
                *part.args.defaults,
                *(value for value in part.args.kw_defaults if value is not None),
            ]
            if not postponed_annotations:
                expressions.extend(
                    arg.annotation
                    for arg in [
                        *part.args.posonlyargs, *part.args.args, *part.args.kwonlyargs
                    ]
                    if arg.annotation is not None
                )
                if part.args.vararg and part.args.vararg.annotation is not None:
                    expressions.append(part.args.vararg.annotation)
                if part.args.kwarg and part.args.kwarg.annotation is not None:
                    expressions.append(part.args.kwarg.annotation)
                if part.returns is not None:
                    expressions.append(part.returns)
            for expression in expressions:
                visit(expression)
            return
        if isinstance(part, ast.ClassDef):
            names.add(part.name)
            # Class attributes have their own namespace, except explicit global
            # writes/deletes in the executing class suite.
            for expression in [*part.decorator_list, *part.bases, *(kw.value for kw in part.keywords)]:
                visit(expression)
            names.update(_class_scope_global_mutations(part, postponed_annotations=postponed_annotations))
            return
        if isinstance(part, (ast.Import, ast.ImportFrom)):
            names.update(_defined_names(part))
            return
        if isinstance(part, ast.comprehension):
            # Comprehension targets are local to the implicit comprehension
            # scope; iterables and filters can still contain outer-scope walruses.
            visit(part.iter)
            for condition in part.ifs:
                visit(condition)
            return
        if isinstance(part, ast.ExceptHandler):
            if part.name:
                names.add(part.name)
            if part.type is not None:
                visit(part.type)
            for statement in part.body:
                visit(statement)
            return
        if isinstance(part, ast.If):
            # Static conditions make one branch unreachable; inspect the test
            # (a walrus may still bind in the containing module) and only the
            # branch Python can execute.
            condition_known = False
            try:
                condition = ast.literal_eval(part.test)
                condition_known = True
            except (ValueError, TypeError):
                condition = None
            visit(part.test)
            selected = (part.body if bool(condition) else part.orelse) if condition_known else None
            if selected is not None:
                for statement in selected:
                    visit(statement)
                return
            for statement in [*part.body, *part.orelse]:
                visit(statement)
            return
        if isinstance(part, ast.AnnAssign) and postponed_annotations:
            # With postponed annotations an annotation-only name does not bind,
            # and its annotation expression is never evaluated.
            if part.value is not None:
                visit(part.target)
                visit(part.value)
            elif not isinstance(part.target, ast.Name):
                visit(part.target)
            return
        if isinstance(part, ast.MatchAs) and part.name:
            names.add(part.name)
        elif isinstance(part, ast.MatchStar) and part.name:
            names.add(part.name)
        elif isinstance(part, ast.MatchMapping) and part.rest:
            names.add(part.rest)
        if isinstance(part, ast.Name) and isinstance(part.ctx, (ast.Store, ast.Del)):
            names.add(part.id)
        if isinstance(part, (ast.For, ast.AsyncFor)):
            # Known-empty literal iterables cannot bind their target or execute
            # the loop body. The else clause can still execute.
            try:
                iterable = ast.literal_eval(part.iter)
            except (ValueError, TypeError):
                iterable = None
            if isinstance(iterable, (list, tuple, dict, set, str, bytes)) and not iterable:
                for statement in part.orelse:
                    visit(statement)
                return
        if isinstance(part, ast.While):
            condition_known = False
            try:
                condition = ast.literal_eval(part.test)
                condition_known = True
            except (ValueError, TypeError):
                condition = None
            if condition_known and not bool(condition):
                for statement in part.orelse:
                    visit(statement)
                return
        for child in ast.iter_child_nodes(part):
            visit(child)

    visit(node)
    return names


def _is_legacy_ai_provider_error_stub(node: ast.AST) -> bool:
    """Recognize only the empty legacy exception class replaced by Native AI's alias."""
    if not isinstance(node, ast.ClassDef) or node.name != "AIProviderError":
        return False
    if node.decorator_list or node.keywords or len(node.bases) != 1:
        return False
    if not isinstance(node.bases[0], ast.Name) or node.bases[0].id != "RuntimeError":
        return False
    body = node.body
    if len(body) == 1 and isinstance(body[0], ast.Pass):
        return True
    return (
        len(body) == 2
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
        and isinstance(body[1], ast.Pass)
    )


def transform_ai_providers(commit: str) -> None:
    """Replace hosted adapters while preserving only provably safe legacy bindings."""
    path = ROOT / "app/ai_providers.py"
    if not path.is_file():
        write_text("app/ai_providers.py", canonical_text(commit, "app/ai_providers.py"))
        return
    source = path.read_text(encoding="utf-8")
    if not PROVIDER_RE.search(source):
        # A legacy exception shim may be the only remaining difference in this
        # facade while the wider branch still contains provider references.
        try:
            provider_neutral_tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            return
        if not any(_is_legacy_ai_provider_error_stub(node) for node in provider_neutral_tree.body):
            return

    old_tree = ast.parse(source, filename=str(path))
    postponed_annotations = _uses_postponed_annotations(old_tree)
    canonical = canonical_text(commit, "app/ai_providers.py")
    canonical_tree = ast.parse(canonical, filename="canonical app/ai_providers.py")
    canonical_nodes_by_name: dict[str, ast.AST] = {}
    for canonical_node in canonical_tree.body:
        for defined_name in _defined_names(canonical_node):
            canonical_nodes_by_name.setdefault(defined_name, canonical_node)
    canonical_names = set(canonical_nodes_by_name)
    provider_adapter_names = {
        "_gemini_json", "_groq_json", "_cerebras_json", "_prompt",
        "dual_ai_research_review", "dual_ai_trade_safety_review",
    }
    extras: list[str] = []
    extra_defined_names: list[set[str]] = []
    extra_definition_lines: list[int] = []
    import_nodes: list[ast.AST] = []
    source_lines = source.splitlines(keepends=True)

    literalize_annotations = (
        postponed_annotations and not _uses_postponed_annotations(canonical_tree)
    )
    retained_annotation_names: set[str] = set()

    def annotation_names(node: ast.AST) -> set[str]:
        names: set[str] = set()
        for part in ast.walk(node):
            annotations: list[ast.AST] = []
            if isinstance(part, ast.arg) and part.annotation is not None:
                annotations.append(part.annotation)
            elif isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef)) and part.returns is not None:
                annotations.append(part.returns)
            elif isinstance(part, ast.AnnAssign):
                annotations.append(part.annotation)
            for annotation in annotations:
                names.update(
                    child.id for child in ast.walk(annotation)
                    if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
                )
        return names

    def literalize_deferred_annotations(source_segment: str) -> str:
        """Defer retained annotations as strings without rewriting comments or formatting."""
        try:
            parsed_segment = ast.parse(source_segment, filename=str(path))
        except SyntaxError as exc:
            raise MigrationError(
                "cannot preserve postponed annotations in retained provider helper"
            ) from exc
        if len(parsed_segment.body) != 1:
            raise MigrationError(
                "retained provider helper did not parse as one top-level statement"
            )
        retained_node = parsed_segment.body[0]
        annotations: list[ast.AST] = []
        for part in ast.walk(retained_node):
            if isinstance(part, ast.arg) and part.annotation is not None:
                annotations.append(part.annotation)
            elif isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef)) and part.returns is not None:
                annotations.append(part.returns)
            elif isinstance(part, ast.AnnAssign):
                annotations.append(part.annotation)
        if not annotations:
            return source_segment

        source_lines = source_segment.splitlines(keepends=True)

        def source_offset(line_number: int, byte_column: int) -> int:
            if line_number < 1 or line_number > len(source_lines):
                raise MigrationError("retained annotation source position is outside its helper")
            prefix = "".join(source_lines[:line_number - 1])
            line = source_lines[line_number - 1]
            try:
                column = len(line.encode("utf-8")[:byte_column].decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise MigrationError("retained annotation has an invalid UTF-8 source position") from exc
            return len(prefix) + column

        edits: list[tuple[int, int, str]] = []
        for annotation in annotations:
            expression = ast.get_source_segment(source_segment, annotation)
            if expression is None:
                raise MigrationError("could not recover retained annotation source text")
            start = source_offset(annotation.lineno, annotation.col_offset)
            end = source_offset(annotation.end_lineno, annotation.end_col_offset)
            edits.append((start, end, repr(expression)))
        for start, end, replacement in sorted(edits, key=lambda item: item[0], reverse=True):
            source_segment = source_segment[:start] + replacement + source_segment[end:]
        return source_segment

    for node in old_tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_nodes.append(node)
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        name = _name(node)
        if name in provider_adapter_names:
            if name == "_cerebras_json" and not _is_safe_legacy_cerebras_adapter(node):
                raise MigrationError(
                    "app/ai_providers.py contains an unrecognized provider-bearing helper _cerebras_json; refusing to overwrite it"
                )
            continue
        start_line = min([node.lineno, *[decorator.lineno for decorator in getattr(node, "decorator_list", [])]])
        segment = "".join(source_lines[start_line - 1:node.end_lineno]).rstrip("\n")
        if name == "AIProviderError" and _is_legacy_ai_provider_error_stub(node):
            # Only replace the exact empty compatibility stub when RuntimeError still
            # resolves to Python's built-in exception class. A module-level binding can
            # make this a custom branch exception whose behavior must be preserved.
            runtime_error_shadowed = False
            for candidate in old_tree.body:
                if candidate is node:
                    break
                if "RuntimeError" in _defined_names(candidate):
                    runtime_error_shadowed = True
                    break
                # Bindings inside module-level control flow are not reported by
                # _defined_names(control_flow_node), but can still change the
                # name resolved by the class base below.
                def module_flow_can_bind_runtime_error(scope: ast.AST) -> bool:
                    pending = [scope]
                    while pending:
                        nested = pending.pop()
                        if "RuntimeError" in _defined_names(nested):
                            return True
                        if (
                            isinstance(nested, ast.Name)
                            and nested.id == "RuntimeError"
                            and isinstance(nested.ctx, (ast.Store, ast.Del))
                        ):
                            return True
                        if isinstance(nested, ast.alias) and (
                            nested.asname == "RuntimeError"
                            or (nested.name == "RuntimeError" and nested.asname is None)
                        ):
                            return True
                        if isinstance(nested, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                            # Local function/lambda assignments do not bind the module name.
                            # A global declaration is a potentially relevant escape, so fail closed.
                            if any(
                                isinstance(item, ast.Global) and "RuntimeError" in item.names
                                for item in ast.walk(nested)
                            ):
                                return True
                            continue
                        if isinstance(nested, ast.ClassDef):
                            # Class-local assignments do not rebind module RuntimeError,
                            # except for an explicit global declaration in the class body.
                            if any(
                                isinstance(item, ast.Global) and "RuntimeError" in item.names
                                for item in ast.walk(nested)
                            ):
                                return True
                            continue
                        pending.extend(ast.iter_child_nodes(nested))
                    return False

                if isinstance(candidate, (ast.If, ast.For, ast.AsyncFor, ast.While,
                                          ast.With, ast.AsyncWith, ast.Try, ast.Match)) and module_flow_can_bind_runtime_error(candidate):
                    runtime_error_shadowed = True
                    break
            if runtime_error_shadowed:
                raise MigrationError(
                    "legacy AIProviderError base RuntimeError is imported or rebound in this facade; refusing to replace a possibly custom exception"
                )
            continue
        defined_names = _defined_names(node) | _module_scope_maybe_binds(node, postponed_annotations=postponed_annotations)
        collisions = defined_names & canonical_names
        if collisions:
            equivalent = all(
                ast.dump(node, include_attributes=False)
                == ast.dump(canonical_nodes_by_name[collision], include_attributes=False)
                for collision in collisions
            )
            if equivalent:
                # AST equality does not capture module-level import bindings. Validate
                # parser helpers before accepting syntactic equality so a branch alias
                # such as "import orjson as json" cannot bypass the dependency guard.
                if name in {"_extract_json", "_response_json_object"} and not _safe_canonical_json_helper(
                    node, name, segment, old_tree
                ):
                    collision = sorted(collisions)[0]
                    raise MigrationError(
                        f"app/ai_providers.py has branch-specific definition {collision!r} that collides with a canonical name; refusing to discard or shadow it"
                    )
                continue
            # The provider-era facade may also carry an exact duplicate of the
            # canonical local safety-payload validator. Accept only its proven AST
            # equivalent; additional checks or side effects still fail closed.
            if (
                len(collisions) == 1
                and next(iter(collisions)) == name == "_validate_trade_safety_payload"
                and _safe_canonical_trade_safety_validator(
                    node, canonical_nodes_by_name[name], segment
                )
            ):
                continue
            # The canonical facade deliberately provides safer implementations of these
            # generic parsers (same callable contract, broader malformed-input handling).
            # Accept only tightly whitelisted legacy parser bodies; do not generalize this
            # exception to arbitrary same-named branch functions.
            if len(collisions) == 1 and next(iter(collisions)) == name and _safe_canonical_json_helper(
                node, name or "", segment, old_tree
            ):
                continue
            collision = sorted(
                name for name in collisions
                if ast.dump(node, include_attributes=False)
                != ast.dump(canonical_nodes_by_name[name], include_attributes=False)
            )[0]
            raise MigrationError(
                f"app/ai_providers.py has branch-specific definition {collision!r} that collides with a canonical name; refusing to discard or shadow it"
            )
        if PROVIDER_RE.search(segment):
            raise MigrationError(
                f"app/ai_providers.py contains an unrecognized provider-bearing helper {name or type(node).__name__}; refusing to overwrite it"
            )
        if literalize_annotations:
            # Annotation expressions become string constants below; retain their
            # source-level names for import selection and typing.get_type_hints.
            retained_annotation_names.update(annotation_names(node))
        extras.append(
            literalize_deferred_annotations(segment).rstrip()
            if literalize_annotations else segment.rstrip()
        )
        extra_defined_names.append(defined_names)
        extra_definition_lines.append(node.lineno)

    used_names: set[str] = set()
    for segment in extras:
        tree = ast.parse(segment)
        used_names.update(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))
    used_names.update(retained_annotation_names)

    def import_signature(import_node: ast.AST, alias: ast.alias) -> tuple[str, int, str, str, str]:
        if isinstance(import_node, ast.Import):
            bound = alias.asname or alias.name.split(".", 1)[0]
            return ("import", 0, "", alias.name, bound)
        if isinstance(import_node, ast.ImportFrom):
            bound = alias.asname or alias.name
            return ("from", import_node.level, import_node.module or "", alias.name, bound)
        raise MigrationError("unexpected non-import node while checking retained imports")

    def import_bindings(tree: ast.Module) -> dict[str, set[tuple[str, int, str, str, str]]]:
        bindings: dict[str, set[tuple[str, int, str, str, str]]] = {}
        for import_node in tree.body:
            if not isinstance(import_node, (ast.Import, ast.ImportFrom)):
                continue
            for alias in import_node.names:
                if alias.name == "*":
                    continue
                signature = import_signature(import_node, alias)
                bindings.setdefault(signature[4], set()).add(signature)
        return bindings

    canonical_imports = import_bindings(canonical_tree)

    def star_import_exports(
        star_node: ast.ImportFrom,
    ) -> tuple[set[str], dict[str, tuple[str, int, str, str, str] | None]]:
        """Statically enumerate star exports and identify simple import-backed bindings."""
        if star_node.level:
            module_base = ROOT / "app"
            for _ in range(star_node.level - 1):
                module_base = module_base.parent
            if star_node.module:
                module_base = module_base.joinpath(*star_node.module.split("."))
            candidates = (
                [module_base / "__init__.py", module_base.with_suffix(".py")]
                if star_node.module else [module_base / "__init__.py"]
            )
        else:
            module_name = star_node.module or ""
            if module_name == "app":
                module_base = ROOT / "app"
                candidates = [module_base / "__init__.py", ROOT / "app.py"]
            else:
                if module_name.startswith("app."):
                    package_module = module_name[4:]
                    module_base = ROOT / "app" / Path(*package_module.split("."))
                else:
                    module_base = ROOT.joinpath(*module_name.split(".")) if module_name else ROOT
                candidates = (
                    [module_base / "__init__.py", module_base.with_suffix(".py")]
                    if module_name else []
                )
        module_file = next((candidate for candidate in candidates if candidate.is_file()), None)
        if module_file is None:
            raise MigrationError(
                f"cannot statically verify star-import exports for {star_node.module!r}; refusing to change retained helper bindings"
            )
        try:
            module_tree = ast.parse(module_file.read_text(encoding="utf-8"), filename=str(module_file))
        except (OSError, SyntaxError) as exc:
            raise MigrationError(
                f"cannot inspect star-import module {module_file}: {exc}"
            ) from exc

        all_values: list[ast.AST | None] = []
        dynamic_all = False
        has_nested_star = False
        dynamic_exports = any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {"__getattr__", "__dir__"}
            for node in module_tree.body
        )

        def root_name(node: ast.AST) -> str | None:
            while isinstance(node, (ast.Attribute, ast.Subscript)):
                node = node.value
            return node.id if isinstance(node, ast.Name) else None

        def directly_assigns_all(target: ast.AST) -> bool:
            return isinstance(target, ast.Name) and target.id == "__all__"

        def mutates_all_target(target: ast.AST) -> bool:
            return root_name(target) == "__all__" and not directly_assigns_all(target)

        # Top-level control-flow can bind names not represented by _defined_names
        # (e.g. "for helper in values"). Do not infer star exports through such code.
        unsafe_module_statements = (
            ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
            ast.Try, ast.Match,
        )
        if any(isinstance(node, unsafe_module_statements) for node in module_tree.body):
            dynamic_exports = True

        for module_node in module_tree.body:
            if isinstance(module_node, ast.ImportFrom) and any(alias.name == "*" for alias in module_node.names):
                has_nested_star = True

            has_direct_all_assignment = False
            if isinstance(module_node, ast.Assign):
                has_direct_all_assignment = any(directly_assigns_all(target) for target in module_node.targets)
                targets_bind_all = any("__all__" in _assigned_names(target) for target in module_node.targets)
                if has_direct_all_assignment:
                    all_values.append(module_node.value)
                    # Chained assignment (aliases = __all__ = [...]) leaks the same
                    # mutable export list through another binding; reject it rather
                    # than assume the alias will remain unchanged.
                    if len(module_node.targets) != 1:
                        # A chained alias is safe only when the shared object is a
                        # literal immutable tuple of strings. Lists/other expressions
                        # may be mutated through the extra alias after assignment.
                        immutable_tuple = (
                            isinstance(module_node.value, ast.Tuple)
                            and all(
                                isinstance(item, ast.Constant) and isinstance(item.value, str)
                                for item in module_node.value.elts
                            )
                        )
                        if not immutable_tuple:
                            dynamic_all = True
                elif targets_bind_all:
                    # Destructuring/attribute targets that include __all__ are not a
                    # simple literal export declaration we can safely reason about.
                    dynamic_all = True
                if any(mutates_all_target(target) for target in module_node.targets):
                    dynamic_all = True
            elif isinstance(module_node, ast.AnnAssign):
                has_direct_all_assignment = directly_assigns_all(module_node.target)
                if has_direct_all_assignment:
                    all_values.append(module_node.value)
                elif mutates_all_target(module_node.target):
                    dynamic_all = True
            elif isinstance(module_node, ast.AugAssign) and root_name(module_node.target) == "__all__":
                dynamic_all = True
            elif isinstance(module_node, ast.Delete) and any(
                root_name(target) == "__all__" for target in module_node.targets
            ):
                dynamic_all = True

            for nested in ast.walk(module_node):
                # Reading __all__ can leak its mutable list through an alias
                # (e.g. names = __all__; names.append(...)). Reject that escape
                # rather than guessing the final export set.
                if (
                    isinstance(nested, ast.Name)
                    and nested.id == "__all__"
                    and isinstance(nested.ctx, ast.Load)
                ):
                    dynamic_all = True

                if isinstance(nested, ast.Call):
                    # Any globals() access can create or mutate module exports through
                    # a computed name; fail closed even when the call is indirect,
                    # such as globals()["__all__"].append(...).
                    if isinstance(nested.func, ast.Name) and nested.func.id == "globals":
                        dynamic_all = True
                    elif (
                        isinstance(nested.func, ast.Name)
                        and nested.func.id in {"exec", "eval"}
                    ):
                        dynamic_exports = True
                    elif (
                        isinstance(nested.func, ast.Attribute)
                        and root_name(nested.func.value) == "__all__"
                    ):
                        dynamic_all = True
                elif isinstance(nested, ast.NamedExpr):
                    if directly_assigns_all(nested.target):
                        # Only a module-level expression (__all__ := [...]) is an
                        # export declaration. Function-local/nested walruses have a
                        # different scope, so reject rather than misinfer exports.
                        if (
                            isinstance(module_node, ast.Expr)
                            and module_node.value is nested
                            and not has_direct_all_assignment
                        ):
                            all_values.append(nested.value)
                        else:
                            dynamic_all = True
                    elif mutates_all_target(nested.target):
                        dynamic_all = True

        if dynamic_all or dynamic_exports or has_nested_star or len(all_values) > 1:
            raise MigrationError(
                f"star-import module {module_file} has dynamic exports or mutates __all__; refusing to guess its exports"
            )
        explicit_all = bool(all_values)
        if explicit_all:
            value = all_values[0]
            if not isinstance(value, (ast.List, ast.Tuple, ast.Set)) or not all(
                isinstance(item, ast.Constant) and isinstance(item.value, str)
                for item in value.elts
            ):
                raise MigrationError(
                    f"star-import module {module_file} has a non-literal __all__; refusing to guess its exports"
                )
            exports = {item.value for item in value.elts}
        else:
            if has_nested_star or dynamic_exports:
                raise MigrationError(
                    f"star-import module {module_file} has dynamic exports; refusing to guess its bindings"
                )
            if any(
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "exec"
                for node in ast.walk(module_tree)
            ):
                raise MigrationError(
                    f"star-import module {module_file} uses exec; refusing to guess its exports"
                )
            exports = {
                name
                for module_node in module_tree.body
                for name in _defined_names(module_node)
                if not name.startswith("_")
            }

        last_definitions: dict[str, ast.AST] = {}
        deleted_bindings: set[str] = set()
        for module_node in module_tree.body:
            if isinstance(module_node, ast.Delete):
                deleted_names = {
                    name for target in module_node.targets for name in _assigned_names(target)
                }
                deleted_bindings.update(deleted_names)
                for deleted_name in deleted_names:
                    last_definitions.pop(deleted_name, None)
                continue
            for defined_name in _defined_names(module_node):
                last_definitions[defined_name] = module_node
                deleted_bindings.discard(defined_name)
        if explicit_all and deleted_bindings.intersection(exports):
            deleted_exports = sorted(deleted_bindings.intersection(exports))
            raise MigrationError(
                f"star-import module {module_file} declares deleted name(s) in __all__: "
                + ", ".join(deleted_exports)
            )
        if not explicit_all:
            # Default star exports reflect names actually left in the module
            # namespace after top-level deletion, not every name seen earlier.
            exports.difference_update(deleted_bindings)
        bindings: dict[str, tuple[str, int, str, str, str] | None] = {}
        for exported_name in exports:
            definition = last_definitions.get(exported_name)
            signature = None
            if isinstance(definition, (ast.Import, ast.ImportFrom)):
                for alias in definition.names:
                    if alias.name != "*" and import_signature(definition, alias)[4] == exported_name:
                        signature = import_signature(definition, alias)
                        break
            bindings[exported_name] = signature
        return exports, bindings

    preserved_imports: list[str] = []
    preserved_import_bindings: list[set[str]] = []
    preserved_import_lines: list[int] = []
    retained_star_imports: list[str] = []
    retained_star_exports: list[set[str]] = []
    retained_star_lines: list[int] = []
    for node in import_nodes:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            if not extras:
                continue
            source_module = "." * node.level + (node.module or "")
            if PROVIDER_RE.search(source_module):
                raise MigrationError(
                    f"app/ai_providers.py has a provider-bearing star import used alongside retained helpers; refusing to preserve it: {source_module}"
                )
            exported_names, exported_bindings = star_import_exports(node)
            for exported_name in sorted(exported_names & canonical_names):
                signature = exported_bindings.get(exported_name)
                if signature is None or signature not in canonical_imports.get(exported_name, set()):
                    raise MigrationError(
                        f"star import {source_module!r} exports canonical name {exported_name!r} with a different or unknown binding; refusing to change helper semantics"
                    )
            retained_star_imports.append(ast.unparse(node))
            retained_star_exports.append(exported_names)
            retained_star_lines.append(node.lineno)
            continue

        aliases = []
        for alias in node.names:
            bound = alias.asname or (
                alias.name.split(".", 1)[0] if isinstance(node, ast.Import) else alias.name
            )
            if bound not in used_names:
                continue
            source_module = alias.name if isinstance(node, ast.Import) else (node.module or "")
            if PROVIDER_RE.search(alias.name) or PROVIDER_RE.search(source_module):
                raise MigrationError(
                    f"app/ai_providers.py retained helper depends on a provider-bearing import {alias.name!r}; refusing to leave it unresolved"
                )
            if bound in canonical_names:
                signature = import_signature(node, alias)
                if signature in canonical_imports.get(bound, set()):
                    # This import has exactly the same binding as the canonical one.
                    continue
                raise MigrationError(
                    f"app/ai_providers.py retained helper import {alias.name!r} binds canonical name {bound!r} differently; refusing to change helper semantics"
                )
            aliases.append(alias)

        if not aliases:
            continue
        if isinstance(node, ast.Import):
            preserved_imports.append(ast.unparse(ast.Import(names=aliases)))
        else:
            preserved_imports.append(ast.unparse(ast.ImportFrom(
                module=node.module, names=aliases, level=node.level
            )))
        preserved_import_bindings.append({
            alias.asname or (alias.name.split(".", 1)[0] if isinstance(node, ast.Import) else alias.name)
            for alias in aliases
        })
        preserved_import_lines.append(node.lineno)

    # The output format preserves star imports before the canonical body and explicit
    # imports after it. Refuse migrations whenever moving a star import before a retained
    # local definition would reverse which binding wins in the original module.
    for names, definition_line in zip(extra_defined_names, extra_definition_lines):
        for star_exports, star_line in zip(retained_star_exports, retained_star_lines):
            if names & star_exports and definition_line < star_line:
                raise MigrationError(
                    "retained definition precedes a later star import with overlapping exports; "
                    "refusing to change helper bindings"
                )

    # Preserved explicit imports are emitted before retained local definitions. If a
    # later import originally replaced a local name, this output order would reverse
    # which binding a retained helper sees; refuse that migration.
    for bindings, import_line in zip(preserved_import_bindings, preserved_import_lines):
        for names, definition_line in zip(extra_defined_names, extra_definition_lines):
            if bindings & names and definition_line < import_line:
                raise MigrationError(
                    "retained explicit import follows a local definition with an overlapping name; "
                    "refusing to change helper bindings"
                )

    for bindings, explicit_line in zip(preserved_import_bindings, preserved_import_lines):
        for star_exports, star_line in zip(retained_star_exports, retained_star_lines):
            if bindings & star_exports and explicit_line < star_line:
                raise MigrationError(
                    "retained explicit import precedes a star import with overlapping exports; "
                    "refusing to change helper bindings"
                )

    canonical_lines = canonical.splitlines(keepends=True)
    header_end_line = 0
    for index, node in enumerate(canonical_tree.body):
        is_docstring = (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        is_future_import = isinstance(node, ast.ImportFrom) and node.module == "__future__"
        if is_docstring or is_future_import:
            header_end_line = node.end_lineno
        else:
            break
    canonical_header = "".join(canonical_lines[:header_end_line]).rstrip()
    canonical_body = "".join(canonical_lines[header_end_line:]).lstrip("\n")
    if retained_star_imports:
        canonical_pieces = [piece for piece in (canonical_header, *retained_star_imports, canonical_body.rstrip()) if piece.strip()]
        canonical_result = "\n\n".join(canonical_pieces) + "\n"
    else:
        canonical_result = canonical.rstrip()

    pieces = [canonical_result]
    if preserved_imports:
        pieces.append("\n".join(preserved_imports))
    if extras:
        pieces.extend(extras)
    result = "\n\n".join(piece for piece in pieces if piece.strip()) + "\n"
    ast.parse(result, filename=str(path))
    if RETIRED_PROVIDER_RE.search(result):
        raise MigrationError("app/ai_providers.py still contains retired-provider wiring after facade migration")
    path.write_text(result, encoding="utf-8")



def _is_safe_legacy_hosted_strategy_generator(node: ast.AST) -> bool:
    """Recognize only the known legacy Gemini/Groq[/Cerebras] fallback wrapper.

    The wrapper's schema validation and market-context call are deliberately checked
    before replacing it with the canonical Native AI draft builder. Any extra business
    logic, side-effecting expression, decorator, or output field fails closed.
    """
    if not isinstance(node, ast.AsyncFunctionDef) or node.decorator_list:
        return False
    if node.name != "generate_strategy_draft":
        return False
    args = node.args
    if (args.posonlyargs or args.args or args.vararg or args.kwarg
            or [a.arg for a in args.kwonlyargs] != ["prompt", "name", "asset", "symbol", "timeframe"]
            or len(args.kw_defaults) != 5
            or args.kw_defaults[0] is not None or args.kw_defaults[1] is not None
            or any(not isinstance(value, ast.Constant) or value.value is not None for value in args.kw_defaults[2:])):
        return False

    def inert_annotation(value: ast.AST | None) -> bool:
        if value is None or isinstance(value, (ast.Name, ast.Constant)):
            return True
        if isinstance(value, ast.Attribute):
            return inert_annotation(value.value)
        if isinstance(value, ast.Subscript):
            return inert_annotation(value.value) and inert_annotation(value.slice)
        if isinstance(value, (ast.Tuple, ast.List)):
            return all(inert_annotation(item) for item in value.elts)
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitOr):
            return inert_annotation(value.left) and inert_annotation(value.right)
        return False

    if any(not inert_annotation(a.annotation) for a in args.kwonlyargs):
        return False
    if not inert_annotation(node.returns):
        return False

    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]

    def is_name(value: ast.AST | None, expected: str) -> bool:
        return isinstance(value, ast.Name) and value.id == expected

    def is_string(value: ast.AST | None, expected: str) -> bool:
        return isinstance(value, ast.Constant) and value.value == expected

    provider_keys = {
        "gemini": "gemini_api_key",
        "groq": "groq_api_key",
        "cerebras": "cerebras_api_key",
    }

    def is_setting(value: ast.AST | None, field: str) -> bool:
        return isinstance(value, ast.Attribute) and value.attr == field and is_name(value.value, "settings")

    def settings_flag_names(value: ast.AST) -> list[str] | None:
        if not isinstance(value, ast.BoolOp) or not isinstance(value.op, ast.And) or len(value.values) < 2:
            return None
        result: list[str] = []
        for term in value.values:
            if (not isinstance(term, ast.UnaryOp) or not isinstance(term.op, ast.Not)
                    or not isinstance(term.operand, ast.Attribute) or not is_name(term.operand.value, "settings")):
                return None
            key = term.operand.attr
            if key not in set(provider_keys.values()) or key in result:
                return None
            result.append(key)
        return result

    def is_provider_list(value: ast.AST, expected: list[str]) -> bool:
        return isinstance(value, ast.List) and all(is_string(item, item.value) for item in value.elts if isinstance(item, ast.Constant)) and [
            item.value if isinstance(item, ast.Constant) else None for item in value.elts
        ] == expected

    def is_provider_append(statement: ast.stmt, provider: str) -> bool:
        if not isinstance(statement, ast.If) or statement.orelse or len(statement.body) != 1:
            return False
        if not is_setting(statement.test, provider_keys[provider]):
            return False
        expr = statement.body[0]
        return (
            isinstance(expr, ast.Expr) and isinstance(expr.value, ast.Call)
            and isinstance(expr.value.func, ast.Attribute) and expr.value.func.attr == "append"
            and is_name(expr.value.func.value, "provider_order")
            and len(expr.value.args) == 1 and is_string(expr.value.args[0], provider)
            and not expr.value.keywords
        )

    def is_ai_provider_error_raise(statement: ast.stmt, message: str | None = None) -> bool:
        if not isinstance(statement, ast.Raise) or not isinstance(statement.exc, ast.Call):
            return False
        call = statement.exc
        if not is_name(call.func, "AIProviderError") or len(call.args) != 1 or call.keywords:
            return False
        if message is not None:
            return is_string(call.args[0], message) and statement.cause is None
        # The only nonconstant error message permitted is the known provider-error
        # aggregation: prefix + "; ".join(f"{k}: {v}" for k, v in provider_errors.items()).
        allowed_names = {"provider_errors", "k", "v"}
        for item in ast.walk(call.args[0]):
            if isinstance(item, ast.Name) and item.id not in allowed_names:
                return False
            if isinstance(item, ast.Call):
                if isinstance(item.func, ast.Attribute):
                    if item.func.attr not in {"join", "items"}:
                        return False
                else:
                    return False
            if isinstance(item, ast.Attribute) and item.attr not in {"join", "items"}:
                return False
        return statement.cause is None

    if len(body) not in {11, 13}:
        return False
    guard = body[0]
    if not isinstance(guard, ast.If) or guard.orelse or len(guard.body) != 1:
        return False
    missing = settings_flag_names(guard.test)
    if not missing or len(missing) not in {2, 3}:
        return False
    if set(missing) not in (
        {"gemini_api_key", "groq_api_key"},
        {"gemini_api_key", "groq_api_key", "cerebras_api_key"},
    ):
        return False
    if not is_ai_provider_error_raise(guard.body[0], "No AI strategy provider is configured"):
        return False

    errors = body[1]
    if not isinstance(errors, ast.AnnAssign) or not is_name(errors.target, "provider_errors"):
        return False
    if not isinstance(errors.value, ast.Dict) or errors.value.keys or errors.value.values:
        return False
    if not inert_annotation(errors.annotation):
        return False

    if len(body) == 11:
        order = body[2]
        if not isinstance(order, ast.Assign) or len(order.targets) != 1 or not is_name(order.targets[0], "provider_order"):
            return False
        order_value = order.value
        if not (
            isinstance(order_value, ast.IfExp)
            and is_setting(order_value.test, "gemini_api_key")
            and is_provider_list(order_value.body, ["gemini", "groq"])
            and is_provider_list(order_value.orelse, ["groq"])
        ):
            return False
        enabled_both = body[3]
        if (not isinstance(enabled_both, ast.If) or enabled_both.orelse or len(enabled_both.body) != 1
                or not isinstance(enabled_both.test, ast.BoolOp) or not isinstance(enabled_both.test.op, ast.And)
                or len(enabled_both.test.values) != 2
                or not is_setting(enabled_both.test.values[0], "gemini_api_key")
                or not is_setting(enabled_both.test.values[1], "groq_api_key")):
            return False
        assign_order = enabled_both.body[0]
        if (not isinstance(assign_order, ast.Assign) or len(assign_order.targets) != 1
                or not is_name(assign_order.targets[0], "provider_order")
                or not is_provider_list(assign_order.value, ["gemini", "groq"])):
            return False
        position = 4
    else:
        order = body[2]
        if (not isinstance(order, ast.Assign) or len(order.targets) != 1
                or not is_name(order.targets[0], "provider_order")
                or not isinstance(order.value, ast.List) or order.value.elts):
            return False
        if not all(is_provider_append(body[i], provider) for i, provider in zip(
            (3, 4, 5), ("gemini", "groq", "cerebras")
        )):
            return False
        position = 6

    raw = body[position]
    if (not isinstance(raw, ast.AnnAssign) or not is_name(raw.target, "raw")
            or not is_string(raw.value, None) and not (isinstance(raw.value, ast.Constant) and raw.value.value is None)
            or not inert_annotation(raw.annotation)):
        return False
    provider_used = body[position + 1]
    if (not isinstance(provider_used, ast.Assign) or len(provider_used.targets) != 1
            or not is_name(provider_used.targets[0], "provider_used") or not is_string(provider_used.value, "")):
        return False

    loop = body[position + 2]
    if (not isinstance(loop, ast.For) or not is_name(loop.target, "provider")
            or not is_name(loop.iter, "provider_order") or loop.orelse or len(loop.body) != 1
            or not isinstance(loop.body[0], ast.Try)):
        return False
    loop_try = loop.body[0]
    if loop_try.orelse or loop_try.finalbody or len(loop_try.body) != 3 or len(loop_try.handlers) != 1:
        return False
    parse_assign, provider_assign, break_stmt = loop_try.body
    if (not isinstance(parse_assign, ast.Assign) or len(parse_assign.targets) != 1
            or not is_name(parse_assign.targets[0], "raw") or not isinstance(parse_assign.value, ast.Await)
            or not isinstance(parse_assign.value.value, ast.Call)
            or not is_name(parse_assign.value.value.func, "_generate_with_provider")
            or len(parse_assign.value.value.args) != 2
            or not is_name(parse_assign.value.value.args[0], "provider")
            or parse_assign.value.value.keywords):
        return False
    prompt_call = parse_assign.value.value.args[1]
    if (not isinstance(prompt_call, ast.Call) or not is_name(prompt_call.func, "_strategy_prompt")
            or len(prompt_call.args) != 2 or not is_name(prompt_call.args[0], "prompt")
            or not is_name(prompt_call.args[1], "name") or prompt_call.keywords):
        return False
    if (not isinstance(provider_assign, ast.Assign) or len(provider_assign.targets) != 1
            or not is_name(provider_assign.targets[0], "provider_used") or not is_name(provider_assign.value, "provider")
            or not isinstance(break_stmt, ast.Break)):
        return False
    handler = loop_try.handlers[0]
    if (not is_name(handler.type, "Exception") or handler.name != "exc" or len(handler.body) != 1
            or not isinstance(handler.body[0], ast.Assign)):
        return False
    handler_assign = handler.body[0]
    if (len(handler_assign.targets) != 1 or not isinstance(handler_assign.targets[0], ast.Subscript)
            or not is_name(handler_assign.targets[0].value, "provider_errors")
            or not is_name(handler_assign.targets[0].slice, "provider")
            or not isinstance(handler_assign.value, ast.Call) or not is_name(handler_assign.value.func, "str")
            or len(handler_assign.value.args) != 1 or not is_name(handler_assign.value.args[0], "exc")
            or handler_assign.value.keywords):
        return False

    empty_raw = body[position + 3]
    if (not isinstance(empty_raw, ast.If) or empty_raw.orelse or len(empty_raw.body) != 1
            or not isinstance(empty_raw.test, ast.Compare) or not is_name(empty_raw.test.left, "raw")
            or len(empty_raw.test.ops) != 1 or not isinstance(empty_raw.test.ops[0], ast.Is)
            or len(empty_raw.test.comparators) != 1 or not isinstance(empty_raw.test.comparators[0], ast.Constant)
            or empty_raw.test.comparators[0].value is not None
            or not is_ai_provider_error_raise(empty_raw.body[0])):
        return False

    validation = body[position + 4]
    if (not isinstance(validation, ast.Try) or validation.orelse or validation.finalbody
            or len(validation.body) != 2 or len(validation.handlers) != 1):
        return False
    model_check, context_check = validation.body
    if (not isinstance(model_check, ast.Assign) or len(model_check.targets) != 1
            or not is_name(model_check.targets[0], "spec") or not isinstance(model_check.value, ast.Call)
            or not isinstance(model_check.value.func, ast.Attribute) or model_check.value.func.attr != "model_validate"
            or not is_name(model_check.value.func.value, "AIStrategySpec")
            or len(model_check.value.args) != 1 or not is_name(model_check.value.args[0], "raw")
            or model_check.value.keywords):
        return False
    if (not isinstance(context_check, ast.Assign) or len(context_check.targets) != 1
            or not is_name(context_check.targets[0], "spec") or not isinstance(context_check.value, ast.Call)
            or not is_name(context_check.value.func, "_apply_market_context")
            or len(context_check.value.args) != 1 or not is_name(context_check.value.args[0], "spec")
            or {keyword.arg for keyword in context_check.value.keywords} != {"asset", "symbol", "timeframe"}
            or any(not is_name(next(k.value for k in context_check.value.keywords if k.arg == key), key)
                   for key in ("asset", "symbol", "timeframe"))):
        return False
    validation_handler = validation.handlers[0]
    if (not is_name(validation_handler.type, "Exception") or validation_handler.name != "exc"
            or len(validation_handler.body) != 1 or not isinstance(validation_handler.body[0], ast.Raise)):
        return False
    raise_stmt = validation_handler.body[0]
    if (not isinstance(raise_stmt.exc, ast.Call) or not is_name(raise_stmt.exc.func, "AIProviderError")
            or len(raise_stmt.exc.args) != 1 or raise_stmt.exc.keywords
            or not is_name(raise_stmt.cause, "exc")):
        return False
    validation_message = raise_stmt.exc.args[0]
    if not (
        isinstance(validation_message, ast.JoinedStr)
        and len(validation_message.values) == 2
        and isinstance(validation_message.values[0], ast.Constant)
        and validation_message.values[0].value == "AI strategy failed schema validation: "
        and isinstance(validation_message.values[1], ast.FormattedValue)
        and is_name(validation_message.values[1].value, "exc")
        and validation_message.values[1].conversion == -1
        and validation_message.values[1].format_spec is None
    ):
        return False

    family = body[position + 5]
    if (not isinstance(family, ast.Assign) or len(family.targets) != 1
            or not is_name(family.targets[0], "family_key") or not isinstance(family.value, ast.Subscript)
            or not isinstance(family.value.value, ast.Dict)):
        return False
    family_subscript = family.value
    if not (isinstance(family_subscript.slice, ast.Attribute) and family_subscript.slice.attr == "strategy_family"
            and is_name(family_subscript.slice.value, "spec")):
        return False
    expected_families = {
        "TREND": "trend", "MOMENTUM": "momentum", "BREAKOUT": "breakout",
        "MEAN_REVERSION": "mean_reversion", "ENSEMBLE": "ensemble",
    }
    actual_families = {
        key.value: value.value for key, value in zip(family_subscript.value.keys, family_subscript.value.values)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
        and isinstance(value, ast.Constant) and isinstance(value.value, str)
    }
    if actual_families != expected_families or len(family_subscript.value.keys) != len(expected_families):
        return False

    result = body[position + 6]
    if not isinstance(result, ast.Return) or not isinstance(result.value, ast.Dict):
        return False
    outer = {
        key.value: value for key, value in zip(result.value.keys, result.value.values)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    if set(outer) != {"strategy", "evidence", "ai"} or len(result.value.keys) != 3:
        return False
    if not (isinstance(outer["strategy"], ast.Call) and isinstance(outer["strategy"].func, ast.Attribute)
            and outer["strategy"].func.attr == "model_dump" and is_name(outer["strategy"].func.value, "spec")
            and not outer["strategy"].args and not outer["strategy"].keywords):
        return False
    evidence = outer["evidence"]
    if (not isinstance(evidence, ast.Call) or not is_name(evidence.func, "evidence_for")
            or len(evidence.args) != 1 or not is_name(evidence.args[0], "family_key") or evidence.keywords):
        return False
    ai = outer["ai"]
    if not isinstance(ai, ast.Dict):
        return False
    ai_values = {
        key.value: value for key, value in zip(ai.keys, ai.values)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    expected_keys = {"provider", "model", "fallback_used", "provider_errors", "execution_authority",
                     "requires_backtest", "requires_walk_forward"}
    if set(ai_values) != expected_keys or len(ai.keys) != len(expected_keys):
        return False
    if not is_name(ai_values["provider"], "provider_used") or not is_name(ai_values["provider_errors"], "provider_errors"):
        return False
    fallback = ai_values["fallback_used"]
    if (not isinstance(fallback, ast.Compare) or not is_name(fallback.left, "provider_used")
            or len(fallback.ops) != 1 or not isinstance(fallback.ops[0], ast.NotEq)
            or len(fallback.comparators) != 1 or not is_string(fallback.comparators[0], "gemini")):
        return False
    if not (isinstance(ai_values["execution_authority"], ast.Constant) and ai_values["execution_authority"].value is False):
        return False
    if not (isinstance(ai_values["requires_backtest"], ast.Constant) and ai_values["requires_backtest"].value is True):
        return False
    if not (isinstance(ai_values["requires_walk_forward"], ast.Constant) and ai_values["requires_walk_forward"].value is True):
        return False

    def is_model_setting(value: ast.AST, provider: str) -> bool:
        fields = {
            "gemini": {"gemini_strategy_model", "gemini_model"},
            "groq": {"groq_strategy_model", "groq_model"},
            "cerebras": {"cerebras_model"},
        }[provider]
        return isinstance(value, ast.Attribute) and is_name(value.value, "settings") and value.attr in fields

    def is_provider_test(value: ast.AST, provider: str) -> bool:
        return (
            isinstance(value, ast.Compare) and is_name(value.left, "provider_used")
            and len(value.ops) == 1 and isinstance(value.ops[0], ast.Eq)
            and len(value.comparators) == 1 and is_string(value.comparators[0], provider)
        )

    def is_audited_model_expression(value: ast.AST) -> bool:
        if len(missing) == 2:
            return (
                isinstance(value, ast.IfExp) and is_provider_test(value.test, "gemini")
                and is_model_setting(value.body, "gemini") and is_model_setting(value.orelse, "groq")
            )
        if not isinstance(value, ast.IfExp) or not is_provider_test(value.test, "gemini"):
            return False
        fallback = value.orelse
        return (
            is_model_setting(value.body, "gemini")
            and isinstance(fallback, ast.IfExp) and is_provider_test(fallback.test, "groq")
            and is_model_setting(fallback.body, "groq") and is_model_setting(fallback.orelse, "cerebras")
        )

    if not is_audited_model_expression(ai_values["model"]):
        return False
    return True


def _dotted_ast_name(node: ast.AST) -> str | None:
    """Return a dotted name only for a pure Name/Attribute expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_ast_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _provider_adapter_name(func: ast.AST, module_tree: ast.Module | None = None) -> str | None:
    """Resolve only a known provider adapter function or an imported ai_providers module attribute."""
    allowed = {"_gemini_json", "_groq_json", "_cerebras_json", "_openai_json"}
    if isinstance(func, ast.Name):
        return func.id if func.id in allowed else None
    if not isinstance(func, ast.Attribute) or func.attr not in allowed or module_tree is None:
        return None
    receiver = _dotted_ast_name(func.value)
    if not receiver:
        return None
    for imported in module_tree.body:
        if not isinstance(imported, ast.Import):
            continue
        for alias in imported.names:
            if alias.name not in {"ai_providers", "app.ai_providers"}:
                continue
            bound = alias.asname or alias.name.split(".", 1)[0]
            # An explicit alias is the only name introduced by this import. The
            # original module path is valid only for an unaliased import such as
            # import app.ai_providers (which binds the root app).
            permitted_receivers = {bound} if alias.asname else {bound, alias.name}
            if receiver not in permitted_receivers:
                continue
            # A later top-level assignment/import/delete that can shadow the module
            # prevents us from treating a same-named attribute as a trusted adapter.
            shadowed = False
            for other in module_tree.body:
                if other is imported:
                    continue
                if isinstance(other, ast.Delete):
                    deleted = {
                        part.id for target in other.targets for part in ast.walk(target)
                        if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Del)
                    }
                    if bound in deleted:
                        shadowed = True
                        break
                if isinstance(other, ast.Import):
                    for other_alias in other.names:
                        if other_alias.asname == bound and other_alias.name not in {"ai_providers", "app.ai_providers"}:
                            shadowed = True
                            break
                        if other_alias.asname is None and other_alias.name.split(".", 1)[0] == bound:
                            # Re-importing a submodule of the same package does not
                            # rebind the package root (e.g. import app.config).
                            continue
                    if shadowed:
                        break
                    continue
                if bound in (_defined_names(other) | _module_scope_maybe_binds(other, postponed_annotations=_uses_postponed_annotations(module_tree))):
                    shadowed = True
                    break
            if not shadowed:
                return func.attr
    return None


def _is_safe_legacy_provider_dispatcher(node: ast.AST, module_tree: ast.Module | None = None) -> bool:
    """Recognize the known legacy adapter router without permitting extra behavior."""
    if not isinstance(node, ast.AsyncFunctionDef) or node.name != "_generate_with_provider" or node.decorator_list:
        return False
    args = node.args
    if (args.posonlyargs or [a.arg for a in args.args] != ["provider", "prompt"]
            or args.vararg or args.kwarg or args.kwonlyargs or args.defaults):
        return False

    # Removing this helper also removes annotation evaluation at module import.
    # Resolve builtin names against module-scope writes before allowing subscription:
    # a rebound "list" or "dict" can run custom __class_getitem__ code.
    builtin_annotation_names = {
        "str", "dict", "int", "float", "bool", "list", "tuple",
        "set", "frozenset", "bytes", "object", "type",
    }
    builtin_generic_names = {"dict", "list", "tuple", "set", "frozenset", "type"}

    def runtime_module_bindings(statement: ast.stmt, postponed: bool) -> set[str]:
        bound = _defined_names(statement) | _module_scope_maybe_binds(
            statement, postponed_annotations=postponed
        )
        # A module-level annotation-only declaration such as list: int records an
        # __annotations__ entry but does not assign or rebind the target name.
        # Keep names bound by the annotation expression itself.
        if isinstance(statement, ast.AnnAssign) and statement.value is None:
            target_names = _assigned_names(statement.target)
            annotation_effects = _module_scope_maybe_binds(
                statement.annotation, postponed_annotations=postponed
            )
            bound.difference_update(target_names)
            bound.update(annotation_effects)
        return bound

    def module_binds_before(name: str) -> bool:
        if module_tree is None:
            return True
        target_index = next(
            (index for index, statement in enumerate(module_tree.body) if statement is node),
            None,
        )
        if target_index is None:
            return True
        postponed = _uses_postponed_annotations(module_tree)
        for statement in module_tree.body[:target_index]:
            if name in runtime_module_bindings(statement, postponed):
                return True
        return False

    def typing_any_is_imported_before() -> bool:
        if module_tree is None:
            return False
        target_index = next(
            (index for index, statement in enumerate(module_tree.body) if statement is node),
            None,
        )
        if target_index is None:
            return False
        last_binding: ast.stmt | None = None
        postponed = _uses_postponed_annotations(module_tree)
        for statement in module_tree.body[:target_index]:
            bound = runtime_module_bindings(statement, postponed)
            if "Any" in bound:
                last_binding = statement
        return (
            isinstance(last_binding, ast.ImportFrom)
            and last_binding.module in {"typing", "typing_extensions"}
            and any(
                alias.name == "Any" and (alias.asname or alias.name) == "Any"
                for alias in last_binding.names
            )
        )

    def is_inert_dispatcher_annotation(annotation: ast.AST | None) -> bool:
        if annotation is None:
            return True
        if isinstance(annotation, ast.Name):
            if annotation.id in builtin_annotation_names:
                return not module_binds_before(annotation.id)
            return annotation.id == "Any" and typing_any_is_imported_before()
        if isinstance(annotation, ast.Constant):
            return annotation.value is None or isinstance(annotation.value, str)
        if isinstance(annotation, ast.Subscript):
            if (
                not isinstance(annotation.value, ast.Name)
                or annotation.value.id not in builtin_generic_names
                or module_binds_before(annotation.value.id)
            ):
                return False
            return is_inert_dispatcher_annotation(annotation.slice)
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            # PEP 604 unions are inert only when both sides independently resolve
            # to trusted builtin/imported types or None, never dynamic expressions.
            return (
                is_inert_dispatcher_annotation(annotation.left)
                and is_inert_dispatcher_annotation(annotation.right)
            )
        if isinstance(annotation, (ast.Tuple, ast.List)):
            return all(is_inert_dispatcher_annotation(item) for item in annotation.elts)
        return False

    if (
        any(not is_inert_dispatcher_annotation(argument.annotation)
            for argument in [*args.posonlyargs, *args.args, *args.kwonlyargs])
        or not is_inert_dispatcher_annotation(node.returns)
    ):
        return False

    def is_name(value: ast.AST | None, expected: str) -> bool:
        return isinstance(value, ast.Name) and value.id == expected

    def is_setting(value: ast.AST | None, allowed: set[str]) -> bool:
        return (isinstance(value, ast.Attribute) and is_name(value.value, "settings")
                and value.attr in allowed)

    def is_model_expr(value: ast.AST, provider: str) -> bool:
        fields = {
            "gemini": {"gemini_strategy_model", "gemini_model"},
            "groq": {"groq_strategy_model", "groq_model"},
            "cerebras": {"cerebras_model"},
        }[provider]

        def is_provider_setting(term: ast.AST) -> bool:
            return is_setting(term, fields)

        if is_provider_setting(value):
            return True
        return (
            isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or) and len(value.values) == 2
            and all(is_provider_setting(term) for term in value.values)
        )

    def safe_adapter_call(value: ast.AST, provider: str) -> bool:
        adapter = {"gemini": "_gemini_json", "groq": "_groq_json", "cerebras": "_cerebras_json"}[provider]
        return (
            isinstance(value, ast.Await) and isinstance(value.value, ast.Call)
            and _provider_adapter_name(value.value.func, module_tree) == adapter
            and len(value.value.args) == 1 and is_name(value.value.args[0], "prompt")
            and len(value.value.keywords) == 2
            and {keyword.arg for keyword in value.value.keywords} == {"system_instruction", "model"}
            and all(
                (keyword.arg == "system_instruction" and is_name(keyword.value, "SYSTEM_PROMPT"))
                or (keyword.arg == "model" and is_model_expr(keyword.value, provider))
                for keyword in value.value.keywords
            )
        )

    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    # A one-provider direct adapter is handled separately by transform_strategy_ai,
    # where it can be matched to the generator's literal provider argument. It must
    # not satisfy the dispatcher check for a multi-provider fallback loop.
    if len(body) == 1:
        return False

    if len(body) not in {2, 3}:
        return False
    branches: list[tuple[str, ast.If]] = []
    for statement in body[:-1]:
        if (not isinstance(statement, ast.If) or statement.orelse or len(statement.body) != 1
                or not isinstance(statement.test, ast.Compare) or not is_name(statement.test.left, "provider")
                or len(statement.test.ops) != 1 or not isinstance(statement.test.ops[0], ast.Eq)
                or len(statement.test.comparators) != 1 or not isinstance(statement.test.comparators[0], ast.Constant)
                or not isinstance(statement.test.comparators[0].value, str)
                or statement.test.comparators[0].value not in {"gemini", "groq", "cerebras"}):
            return False
        provider = statement.test.comparators[0].value
        if any(provider == previous for previous, _ in branches):
            return False
        if (not isinstance(statement.body[0], ast.Return)
                or not safe_adapter_call(statement.body[0].value, provider)):
            return False
        branches.append((provider, statement))

    last_return = body[-1]
    if not isinstance(last_return, ast.Return) or not isinstance(last_return.value, ast.Await):
        return False
    last_call = last_return.value.value
    if not isinstance(last_call, ast.Call):
        return False
    if len(body) == 2:
        return (
            branches[0][0] == "gemini"
            and _provider_adapter_name(last_call.func, module_tree) == "_groq_json"
            and len(last_call.args) == 1 and is_name(last_call.args[0], "prompt")
            and len(last_call.keywords) == 2
            and {k.arg for k in last_call.keywords} == {"system_instruction", "model"}
            and all(
                (k.arg == "system_instruction" and is_name(k.value, "SYSTEM_PROMPT"))
                or (k.arg == "model" and is_model_expr(k.value, "groq"))
                for k in last_call.keywords
            )
        )
    branch_names = [name for name, _ in branches]
    if branch_names == ["gemini", "groq"]:
        fallback_provider = "cerebras"
    elif branch_names == ["gemini", "cerebras"]:
        fallback_provider = "groq"
    else:
        return False
    fallback_adapter = {
        "gemini": "_gemini_json",
        "groq": "_groq_json",
        "cerebras": "_cerebras_json",
    }[fallback_provider]
    return (
        _provider_adapter_name(last_call.func, module_tree) == fallback_adapter
        and len(last_call.args) == 1 and is_name(last_call.args[0], "prompt")
        and len(last_call.keywords) == 2
        and {k.arg for k in last_call.keywords} == {"system_instruction", "model"}
        and all(
            (k.arg == "system_instruction" and is_name(k.value, "SYSTEM_PROMPT"))
            or (k.arg == "model" and is_model_expr(k.value, fallback_provider))
            for k in last_call.keywords
        )
    )



def _module_path_for_import(source_path: Path, node: ast.ImportFrom, imported_name: str | None = None) -> Path | None:
    """Resolve a statically named project import without importing application code."""
    if node.level and node.module is None:
        package_dir = source_path.parent
        for _ in range(node.level - 1):
            package_dir = package_dir.parent
        return package_dir / "__init__.py"
    if node.level:
        base = source_path.parent
        for _ in range(node.level - 1):
            base = base.parent
        module_name = node.module
        if not module_name:
            return None
        base = base.joinpath(*module_name.split("."))
    else:
        if not node.module:
            return None
        base = ROOT.joinpath(*node.module.split("."))
    candidates = (base / "__init__.py", base.with_suffix(".py"))
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _resolve_class_definition(
    source_path: Path,
    module_tree: ast.Module,
    symbol_name: str,
    seen: set[tuple[str, str]] | None = None,
) -> tuple[Path, ast.Module, ast.ClassDef] | None:
    """Resolve a local or imported class, including package-level re-exports."""
    seen = set() if seen is None else set(seen)
    key = (str(source_path), symbol_name)
    if key in seen:
        return None
    seen.add(key)
    matches = [node for node in module_tree.body if isinstance(node, ast.ClassDef) and node.name == symbol_name]
    if len(matches) == 1:
        return source_path, module_tree, matches[0]
    if matches:
        return None
    for import_node in module_tree.body:
        if not isinstance(import_node, ast.ImportFrom):
            continue
        for alias in import_node.names:
            if alias.name == "*" or (alias.asname or alias.name) != symbol_name:
                continue
            target_path = _module_path_for_import(source_path, import_node, alias.name)
            if target_path is None or not target_path.is_file():
                continue
            try:
                target_tree = ast.parse(target_path.read_text(encoding="utf-8"), filename=str(target_path))
            except (OSError, SyntaxError):
                return None
            resolved = _resolve_class_definition(target_path, target_tree, alias.name, seen)
            if resolved is not None:
                return resolved
    return None


def _is_unshadowed_pydantic_v2_model(
    schema: ast.ClassDef, schema_path: Path, schema_tree: ast.Module
) -> bool:
    """Prove that the model's BaseModel base is the real Pydantic v2 symbol."""
    if schema.decorator_list or schema.keywords or len(schema.bases) != 1:
        return False
    base = schema.bases[0]
    binding: str | None = None
    allowed_import_ids: set[int] = set()
    if isinstance(base, ast.Name):
        binding = base.id
        for statement in schema_tree.body:
            if isinstance(statement, ast.ImportFrom) and statement.level == 0 and statement.module == "pydantic":
                if any(alias.name == "BaseModel" and (alias.asname or alias.name) == binding for alias in statement.names):
                    allowed_import_ids.add(id(statement))
    elif isinstance(base, ast.Attribute) and base.attr == "BaseModel" and isinstance(base.value, ast.Name):
        binding = base.value.id
        for statement in schema_tree.body:
            if isinstance(statement, ast.Import):
                if any(alias.name == "pydantic" and (alias.asname or alias.name.split(".", 1)[0]) == binding
                       for alias in statement.names):
                    allowed_import_ids.add(id(statement))
    if not binding or not allowed_import_ids:
        return False

    def rebinds(node: ast.AST) -> bool:
        if id(node) in allowed_import_ids:
            return False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return node.name == binding
        if isinstance(node, ast.ImportFrom):
            return any((alias.asname or alias.name) == binding for alias in node.names)
        if isinstance(node, ast.Import):
            return any((alias.asname or alias.name.split(".", 1)[0]) == binding for alias in node.names)
        if isinstance(node, ast.Name) and node.id == binding and isinstance(node.ctx, (ast.Store, ast.Del)):
            return True
        return any(rebinds(child) for child in ast.iter_child_nodes(node))

    return not any(rebinds(statement) for statement in schema_tree.body)


def _is_compatible_strategy_schema_and_context(tree: ast.Module, canonical_tree: ast.Module) -> bool:
    """Verify a legacy schema/context contract without requiring identical source syntax."""

    canonical_schemas = [
        node for node in canonical_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AIStrategySpec"
    ]
    source_path = ROOT / "app" / "strategy_ai.py"
    resolved = _resolve_class_definition(source_path, tree, "AIStrategySpec")
    if len(canonical_schemas) != 1 or resolved is None:
        return False
    schema_path, schema_tree, schema = resolved
    canonical_schema = canonical_schemas[0]
    if not _is_unshadowed_pydantic_v2_model(schema, schema_path, schema_tree):
        return False
    if not any(
        isinstance(base, ast.Name) and base.id == "BaseModel"
        for base in canonical_schema.bases
    ):
        return False

    def body_without_docstring(node: ast.ClassDef) -> list[ast.stmt]:
        body = list(node.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        return body

    canonical_body = body_without_docstring(canonical_schema)
    schema_body = body_without_docstring(schema)

    def is_classvar(annotation: ast.AST) -> bool:
        value = annotation.value if isinstance(annotation, ast.Subscript) else annotation
        return (
            isinstance(value, ast.Name) and value.id == "ClassVar"
        ) or (
            isinstance(value, ast.Attribute) and value.attr == "ClassVar"
        )

    def field_map(body: list[ast.stmt]) -> dict[str, ast.AnnAssign]:
        return {
            statement.target.id: statement
            for statement in body
            if isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and not is_classvar(statement.annotation)
        }

    canonical_fields = field_map(canonical_body)
    fields = field_map(schema_body)
    if not canonical_fields or not set(canonical_fields).issubset(fields):
        return False

    def expand_alias(node: ast.AST, aliases: dict[str, ast.AST], seen_aliases: set[str] | None = None) -> ast.AST:
        seen_aliases = set() if seen_aliases is None else seen_aliases
        if isinstance(node, ast.Name) and node.id in aliases and node.id not in seen_aliases:
            return expand_alias(aliases[node.id], aliases, seen_aliases | {node.id})
        if isinstance(node, ast.Subscript):
            return ast.Subscript(
                value=expand_alias(node.value, aliases, seen_aliases),
                slice=expand_alias(node.slice, aliases, seen_aliases),
                ctx=node.ctx,
            )
        if isinstance(node, ast.BinOp):
            return ast.BinOp(
                left=expand_alias(node.left, aliases, seen_aliases),
                op=node.op,
                right=expand_alias(node.right, aliases, seen_aliases),
            )
        if isinstance(node, ast.Tuple):
            return ast.Tuple(elts=[expand_alias(item, aliases, seen_aliases) for item in node.elts], ctx=node.ctx)
        return node

    def simple_aliases(module: ast.Module) -> dict[str, ast.AST]:
        return {
            target.id: node.value
            for node in module.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, (ast.Name, ast.Subscript, ast.BinOp, ast.Tuple))
            for target in node.targets
        }

    canonical_aliases = simple_aliases(canonical_tree)
    legacy_aliases = simple_aliases(schema_tree)

    def annotation_compatible(legacy_annotation: ast.AST, canonical_annotation: ast.AST) -> bool:
        legacy_norm = expand_alias(legacy_annotation, legacy_aliases)
        canonical_norm = expand_alias(canonical_annotation, canonical_aliases)
        if ast.dump(legacy_norm, include_attributes=False) == ast.dump(canonical_norm, include_attributes=False):
            return True
        if isinstance(legacy_norm, ast.Name) and legacy_norm.id in {"str", "bool", "int", "float"}:
            if isinstance(canonical_norm, ast.Subscript) and isinstance(canonical_norm.value, ast.Name) and canonical_norm.value.id == "Literal":
                return True
        if isinstance(legacy_norm, ast.Subscript) and isinstance(canonical_norm, ast.Subscript):
            if not annotation_compatible(legacy_norm.value, canonical_norm.value):
                return False
            return annotation_compatible(legacy_norm.slice, canonical_norm.slice)
        if isinstance(legacy_norm, ast.BinOp) and isinstance(canonical_norm, ast.BinOp):
            return (
                type(legacy_norm.op) is type(canonical_norm.op)
                and annotation_compatible(legacy_norm.left, canonical_norm.left)
                and annotation_compatible(legacy_norm.right, canonical_norm.right)
            )
        if isinstance(legacy_norm, ast.Tuple) and isinstance(canonical_norm, ast.Tuple):
            return len(legacy_norm.elts) == len(canonical_norm.elts) and all(
                annotation_compatible(a, b) for a, b in zip(legacy_norm.elts, canonical_norm.elts)
            )
        return False

    def field_is_required(statement: ast.AnnAssign) -> bool:
        default = statement.value
        if default is None:
            return True
        if isinstance(default, ast.Call) and isinstance(default.func, ast.Name) and default.func.id == "Field":
            keywords = {keyword.arg: keyword.value for keyword in default.keywords}
            if "default_factory" in keywords:
                return False
            if "default" in keywords:
                value = keywords["default"]
                return isinstance(value, ast.Constant) and value.value is Ellipsis
            if default.args:
                return isinstance(default.args[0], ast.Constant) and default.args[0].value is Ellipsis
            return True
        return False

    def field_has_nondefault_constraints(statement: ast.AnnAssign) -> bool:
        value = statement.value
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name) or value.func.id != "Field":
            return False
        return bool(value.args) or any(keyword.arg not in {"default", "default_factory"} for keyword in value.keywords)

    for name, canonical_field in canonical_fields.items():
        legacy_field = fields[name]
        if not annotation_compatible(legacy_field.annotation, canonical_field.annotation):
            return False
        # A weaker legacy declaration must not bypass canonical length/range constraints
        # or make a required canonical field optional.
        if field_has_nondefault_constraints(canonical_field) and ast.dump(
            legacy_field, include_attributes=False
        ) != ast.dump(canonical_field, include_attributes=False):
            return False
        if field_has_nondefault_constraints(legacy_field) and ast.dump(
            legacy_field, include_attributes=False
        ) != ast.dump(canonical_field, include_attributes=False):
            return False
        if field_is_required(canonical_field) and not field_is_required(legacy_field):
            return False

    for name, statement in fields.items():
        if name not in canonical_fields and field_is_required(statement):
            return False

    canonical_methods = {
        node.name: node for node in canonical_body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    methods = {
        node.name: node for node in schema_body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    # Never retain a schema with a missing validator: checking only methods that
    # happen to exist permits a branch to remove canonical symbol/timeframe/risk gates.
    if any(
        name not in methods
        or ast.dump(methods[name], include_attributes=False)
        != ast.dump(canonical_method, include_attributes=False)
        for name, canonical_method in canonical_methods.items()
    ):
        return False
    if any(
        name not in canonical_methods
        or ast.dump(method, include_attributes=False)
        != ast.dump(canonical_methods[name], include_attributes=False)
        for name, method in methods.items()
    ):
        return False

    def model_config_node(body: list[ast.stmt]) -> ast.stmt | None:
        matches = [
            statement for statement in body
            if isinstance(statement, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "model_config" for target in statement.targets)
        ]
        return matches[0] if len(matches) == 1 else None

    canonical_config = model_config_node(canonical_body)
    config = model_config_node(schema_body)
    if config is not None and (canonical_config is None or ast.dump(config, include_attributes=False) != ast.dump(canonical_config, include_attributes=False)):
        return False

    allowed_ids = {id(node) for node in fields.values()} | {id(node) for node in methods.values()}
    if config is not None:
        allowed_ids.add(id(config))
    if any(id(statement) not in allowed_ids for statement in schema_body):
        return False

    contexts = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_apply_market_context"
    ]
    canonical_contexts = [
        node for node in canonical_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_apply_market_context"
    ]
    if len(contexts) != 1 or len(canonical_contexts) != 1:
        return False

    def _rooted_name(node: ast.AST) -> str | None:
        while isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        return node.id if isinstance(node, ast.Name) else None

    def _module_flow_rebinds_or_mutates(node: ast.AST, name: str) -> bool:
        """Detect conditional stores, aliases, and expression-level mutations of a module binding."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return True
            # A function with global declaration can rebind or mutate the module value later.
            return any(isinstance(part, ast.Global) and name in part.names for part in ast.walk(node))
        if isinstance(node, ast.ClassDef):
            if node.name == name:
                return True
            if any(isinstance(part, ast.Global) and name in part.names for part in ast.walk(node)):
                return True
            # Class bodies execute at import time, unlike ordinary function bodies.
            # Inspect bases, decorators, and every class-body statement for writes or
            # calls that can alter a dependent module object (for example settings).
            return any(_module_flow_rebinds_or_mutates(part, name) for part in ast.iter_child_nodes(node))
        if isinstance(node, ast.Lambda):
            return False
        if isinstance(node, ast.Import):
            return any((alias.asname or alias.name.split(".", 1)[0]) == name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            # A star import can overwrite any dependent global without naming it here.
            # Reject it rather than claiming the previously inspected binding survives.
            if any(alias.name == "*" for alias in node.names):
                return True
            return any((alias.asname or alias.name) == name for alias in node.names)
        if isinstance(node, ast.Assign):
            if any(name in _assigned_names(target) or _rooted_name(target) == name for target in node.targets):
                return True
            # A direct alias can expose a mutable dependent object to later code;
            # a mere use as a call target (e.g. ConfigDict(...)) is not a mutation.
            return (
                isinstance(node.value, (ast.Name, ast.Attribute, ast.Subscript))
                and _rooted_name(node.value) == name
            )
        if isinstance(node, ast.AnnAssign):
            if name in _assigned_names(node.target) or _rooted_name(node.target) == name:
                return True
            # An annotation/default may legitimately *use* a dependency (e.g. Field(...)).
            # Only a direct value alias exposes the original dependency object to later
            # mutation; nested calls and validator declarations are reads, not rebinding.
            return (
                node.value is not None
                and isinstance(node.value, (ast.Name, ast.Attribute, ast.Subscript))
                and _rooted_name(node.value) == name
            )
        if isinstance(node, ast.AugAssign):
            return name in _assigned_names(node.target) or _rooted_name(node.target) == name
        if isinstance(node, ast.Delete):
            return any(name in _assigned_names(target) or _rooted_name(target) == name for target in node.targets)
        if isinstance(node, ast.NamedExpr):
            return name in _assigned_names(node.target) or _rooted_name(node.target) == name
        if isinstance(node, ast.Expr):
            # A module-scope expression that touches the dependent object could mutate it
            # (e.g. ALLOWED_TIMEFRAMES.clear()); conservatively refuse to infer it safe.
            return any(isinstance(part, ast.Name) and part.id == name and isinstance(part.ctx, ast.Load)
                       for part in ast.walk(node))
        if isinstance(node, (ast.For, ast.AsyncFor)):
            if name in _assigned_names(node.target) or _rooted_name(node.target) == name:
                return True
        if isinstance(node, (ast.With, ast.AsyncWith)):
            if any(item.optional_vars is not None and
                   (name in _assigned_names(item.optional_vars) or _rooted_name(item.optional_vars) == name)
                   for item in node.items):
                return True
        if isinstance(node, ast.ExceptHandler) and node.name == name:
            return True
        if isinstance(node, ast.MatchAs) and node.name == name:
            return True
        if isinstance(node, ast.MatchStar) and node.name == name:
            return True
        if isinstance(node, ast.MatchMapping) and node.rest == name:
            return True
        return any(_module_flow_rebinds_or_mutates(child, name) for child in ast.iter_child_nodes(node))

    def module_binding_signature(module: ast.Module, name: str) -> tuple[str, ...] | None:
        """Return one statically proven module binding, rejecting aliases and conditional mutations."""
        signatures: list[tuple[str, ...]] = []
        binding_statement_ids: set[int] = set()
        for statement in module.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if statement.name == name:
                    signatures.append((type(statement).__name__, ast.dump(statement, include_attributes=False)))
                    binding_statement_ids.add(id(statement))
            elif isinstance(statement, ast.ImportFrom):
                for alias in statement.names:
                    if alias.name != "*" and (alias.asname or alias.name) == name:
                        signatures.append(("ImportFrom", str(statement.level), statement.module or "", alias.name, alias.asname or ""))
                        binding_statement_ids.add(id(statement))
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    if (alias.asname or alias.name.split(".", 1)[0]) == name:
                        signatures.append(("Import", alias.name, alias.asname or ""))
                        binding_statement_ids.add(id(statement))
            elif isinstance(statement, ast.Assign):
                if any(name in _assigned_names(target) or _rooted_name(target) == name for target in statement.targets):
                    signatures.append(("Assign", ast.dump(statement, include_attributes=False)))
                    binding_statement_ids.add(id(statement))
            elif isinstance(statement, ast.AnnAssign):
                if name in _assigned_names(statement.target) or _rooted_name(statement.target) == name:
                    signatures.append(("AnnAssign", ast.dump(statement, include_attributes=False)))
                    binding_statement_ids.add(id(statement))
            elif isinstance(statement, (ast.AugAssign, ast.Delete)):
                targets = [statement.target] if isinstance(statement, ast.AugAssign) else statement.targets
                if any(name in _assigned_names(target) or _rooted_name(target) == name for target in targets):
                    signatures.append((type(statement).__name__, ast.dump(statement, include_attributes=False)))
                    binding_statement_ids.add(id(statement))

        if len(signatures) != 1:
            return None
        # A second assignment hidden inside if/try/loop/match or a module-level expression
        # that mutates the object invalidates the otherwise direct binding.
        for statement in module.body:
            if id(statement) in binding_statement_ids:
                continue
            if _module_flow_rebinds_or_mutates(statement, name):
                return None
        return signatures[0]

    def dependent_global_names(*nodes: ast.AST) -> set[str] | None:
        try:
            isolated = ast.Module(body=list(nodes), type_ignores=[])
            table = symtable.symtable(ast.unparse(isolated), "<strategy-contract>", "exec")
        except (SyntaxError, ValueError):
            return None
        names: set[str] = set()

        def visit(symbol_table: symtable.SymbolTable) -> None:
            for symbol in symbol_table.get_symbols():
                if symbol.is_referenced() and symbol.is_global():
                    names.add(symbol.get_name())
            for child in symbol_table.get_children():
                visit(child)

        visit(table)
        return names

    # The schema class is resolved from its defining module, which may differ from the
    # strategy module containing _apply_market_context. Resolve each set of globals in
    # the module where its referencing code actually executes.
    schema_dependencies = dependent_global_names(schema)
    context_dependencies = dependent_global_names(contexts[0])
    if schema_dependencies is None or context_dependencies is None:
        return False

    # The schema's base-class name may be a proven alias of pydantic.BaseModel.
    # The helper above already proves its import origin and absence of shadowing; it
    # must not then be rejected merely because canonical source uses another spelling.
    proven_pydantic_base_aliases = {
        schema.bases[0].id
    } if isinstance(schema.bases[0], ast.Name) else set()
    replacement_import_names = {"NativeAIError", "compose_strategy_spec"}
    for source_module, dependent_names in (
        (schema_tree, schema_dependencies),
        (tree, context_dependencies),
    ):
        for dependency in dependent_names:
            # transform_strategy_ai inserts both names before the retained schema;
            # a BaseModel alias with either name would be overwritten at runtime.
            if (
                source_module is schema_tree
                and dependency in proven_pydantic_base_aliases
                and dependency not in replacement_import_names
            ):
                continue
            legacy_binding = module_binding_signature(source_module, dependency)
            canonical_binding = module_binding_signature(canonical_tree, dependency)
            if dependency == "AIStrategySpec":
                # The legacy schema may have optional extensions, and the context may
                # import the class from its defining module. Require unambiguous bindings
                # without requiring an imported class to have the canonical local AST.
                if legacy_binding is None or module_binding_signature(tree, dependency) is None:
                    return False
                continue
            if dependency in dir(builtins) and legacy_binding is None and canonical_binding is None:
                # A built-in is safe only when neither source module shadows it.
                continue
            if legacy_binding is None or legacy_binding != canonical_binding:
                return False

    # The field/method/configuration checks above already validate a semantically
    # compatible schema. If its context binder is byte-for-byte canonical, do not
    # re-interpret that known-safe AST through the stricter legacy-shape fallback.
    if ast.dump(contexts[0], include_attributes=False) == ast.dump(
        canonical_contexts[0], include_attributes=False
    ):
        return True
    context = contexts[0]
    args = context.args
    if (
        context.decorator_list or args.posonlyargs or [arg.arg for arg in args.args] != ["spec"]
        or args.vararg or args.kwarg or [arg.arg for arg in args.kwonlyargs] != ["asset", "symbol", "timeframe"]
        or args.defaults or len(args.kw_defaults) != 3
        or any(not isinstance(value, ast.Constant) or value.value is not None for value in args.kw_defaults)
        or not context.body or not isinstance(context.body[-1], ast.Return)
    ):
        return False

    returns = [node for node in ast.walk(context) if isinstance(node, ast.Return)]
    if len(returns) != 1 or returns[0] is not context.body[-1]:
        return False
    result = returns[0].value
    if (
        not isinstance(result, ast.Call) or not isinstance(result.func, ast.Attribute)
        or not isinstance(result.func.value, ast.Name) or result.func.value.id != "AIStrategySpec"
        or result.func.attr != "model_validate" or len(result.args) != 1
        or not isinstance(result.args[0], ast.Name) or result.args[0].id != "data" or result.keywords
    ):
        return False
    assignments = [
        statement for statement in context.body
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name) and statement.targets[0].id == "data"
        and isinstance(statement.value, ast.Call) and isinstance(statement.value.func, ast.Attribute)
        and isinstance(statement.value.func.value, ast.Name) and statement.value.func.value.id == "spec"
        and statement.value.func.attr == "model_dump" and not statement.value.args and not statement.value.keywords
    ]
    if len(assignments) != 1 or assignments[0].lineno >= returns[0].lineno:
        return False
    # The dump assignment must be the first executable statement: otherwise an
    # accepted market guard may reference data before it exists.
    executable_body = list(context.body)
    if executable_body and isinstance(executable_body[0], ast.Expr) and isinstance(
        executable_body[0].value, ast.Constant
    ) and isinstance(executable_body[0].value.value, str):
        executable_body = executable_body[1:]
    if not executable_body or executable_body[0] is not assignments[0]:
        return False

    # All three user-selected market dimensions must be written into model data.
    # Otherwise the rewritten generator can validate a draft for a different market.
    bound_market_fields: set[str] = set()
    for statement in context.body:
        if statement is assignments[0] or statement is returns[0]:
            continue
        if not isinstance(statement, ast.If) or statement.orelse or len(statement.body) != 1:
            return False
        test = statement.test
        if not (
            isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.IsNot)
            and len(test.comparators) == 1 and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None and isinstance(test.left, ast.Name)
            and test.left.id in {"asset", "symbol", "timeframe"}
        ):
            return False
        assignment = statement.body[0]
        if not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Subscript):
            return False
        target = assignment.targets[0]
        if not isinstance(target.value, ast.Name) or target.value.id != "data" or not isinstance(target.slice, ast.Constant):
            return False
        expected_key = {"asset": "asset", "symbol": "symbols", "timeframe": "timeframes"}[test.left.id]
        if target.slice.value != expected_key:
            return False
        if test.left.id in {"asset", "timeframe"}:
            if not isinstance(assignment.value, ast.Name) or assignment.value.id != test.left.id:
                return False
        else:
            if not isinstance(assignment.value, ast.List) or len(assignment.value.elts) != 1 or not isinstance(assignment.value.elts[0], ast.Name) or assignment.value.elts[0].id != "symbol":
                return False
        bound_market_fields.add(test.left.id)
    return bound_market_fields == {"asset", "symbol", "timeframe"}


def _is_safe_legacy_cerebras_adapter(node: ast.AST) -> bool:
    """Allow only the audited Cerebras HTTP/JSON adapter, never arbitrary same-named helpers."""
    if not isinstance(node, ast.AsyncFunctionDef) or node.name != "_cerebras_json" or node.decorator_list:
        return False
    args = node.args
    if (
        args.posonlyargs or [arg.arg for arg in args.args] != ["prompt"]
        or args.vararg or args.kwarg or [arg.arg for arg in args.kwonlyargs] != ["timeout", "system_instruction", "model"]
        or len(args.kw_defaults) != 3
        or not isinstance(args.kw_defaults[0], ast.Constant) or args.kw_defaults[0].value != 30.0
        or not isinstance(args.kw_defaults[1], ast.Constant) or args.kw_defaults[1].value is not None
        or not isinstance(args.kw_defaults[2], ast.Constant) or args.kw_defaults[2].value is not None
    ):
        return False

    def inert_type_annotation(value: ast.AST | None) -> bool:
        if value is None:
            return True
        if isinstance(value, ast.Constant):
            return value.value is None
        if isinstance(value, ast.Name):
            return value.id in {"str", "float", "dict", "Any"}
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitOr):
            return inert_type_annotation(value.left) and inert_type_annotation(value.right)
        if isinstance(value, ast.Subscript):
            return inert_type_annotation(value.value) and inert_type_annotation(value.slice)
        if isinstance(value, (ast.Tuple, ast.List)):
            return all(inert_type_annotation(item) for item in value.elts)
        return False

    parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg is not None:
        parameters.append(args.vararg)
    if args.kwarg is not None:
        parameters.append(args.kwarg)
    if any(not inert_type_annotation(parameter.annotation) for parameter in parameters):
        return False
    if not inert_type_annotation(node.returns):
        return False

    def is_name(value: ast.AST | None, expected: str) -> bool:
        return isinstance(value, ast.Name) and value.id == expected

    def is_string(value: ast.AST | None, expected: str) -> bool:
        return isinstance(value, ast.Constant) and value.value == expected

    def is_attr(value: ast.AST | None, base: str, attr: str) -> bool:
        return isinstance(value, ast.Attribute) and value.attr == attr and is_name(value.value, base)

    def dict_entries(value: ast.AST, expected_keys: set[str]) -> dict[str, ast.AST] | None:
        if not isinstance(value, ast.Dict):
            return None
        entries = {
            key.value: item for key, item in zip(value.keys, value.values)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if set(entries) != expected_keys or len(value.keys) != len(expected_keys):
            return None
        return entries

    def is_provider_key_guard(statement: ast.stmt) -> bool:
        if not isinstance(statement, ast.If) or statement.orelse or len(statement.body) != 1:
            return False
        test = statement.test
        if not (
            isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
            and is_attr(test.operand, "settings", "cerebras_api_key")
        ):
            return False
        raised = statement.body[0]
        return (
            isinstance(raised, ast.Raise) and isinstance(raised.exc, ast.Call)
            and is_name(raised.exc.func, "AIProviderError") and len(raised.exc.args) == 1
            and is_string(raised.exc.args[0], "CEREBRAS_API_KEY is not configured")
            and not raised.exc.keywords and raised.cause is None
        )

    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    if len(body) != 7:
        return False
    if not is_provider_key_guard(body[0]):
        return False
    url_assign, payload_assign, headers_assign, request_context, extract_try, result_return = (
        body[1], body[2], body[3], body[4], body[5], body[6]
    )
    if (
        not isinstance(url_assign, ast.Assign) or len(url_assign.targets) != 1
        or not is_name(url_assign.targets[0], "url")
        or not is_string(url_assign.value, "https://api.cerebras.ai/v1/chat/completions")
    ):
        return False

    if not isinstance(payload_assign, ast.Assign) or len(payload_assign.targets) != 1 or not is_name(payload_assign.targets[0], "payload"):
        return False
    payload = dict_entries(payload_assign.value, {"model", "messages", "temperature", "response_format"})
    if payload is None:
        return False
    model_expr = payload["model"]
    if not (
        isinstance(model_expr, ast.BoolOp) and isinstance(model_expr.op, ast.Or) and len(model_expr.values) == 2
        and is_name(model_expr.values[0], "model") and is_attr(model_expr.values[1], "settings", "cerebras_model")
    ):
        return False
    messages = payload["messages"]
    if not isinstance(messages, ast.List) or len(messages.elts) != 2:
        return False
    system_message = dict_entries(messages.elts[0], {"role", "content"})
    user_message = dict_entries(messages.elts[1], {"role", "content"})
    if system_message is None or user_message is None:
        return False
    if not is_string(system_message["role"], "system") or not is_string(user_message["role"], "user"):
        return False
    system_content = system_message["content"]
    if (
        not isinstance(system_content, ast.BoolOp) or not isinstance(system_content.op, ast.Or)
        or len(system_content.values) != 2 or not is_name(system_content.values[0], "system_instruction")
        or not is_string(
            system_content.values[1],
            "You are Atlas Trading OS's research assistant. Analyze supplied research only. "
            "Do not place trades, invent market data, or give guaranteed outcomes. Return JSON only.",
        )
    ):
        return False
    if not is_name(user_message["content"], "prompt"):
        return False
    if not (isinstance(payload["temperature"], ast.Constant) and payload["temperature"].value == 0.1):
        return False
    response_format = dict_entries(payload["response_format"], {"type"})
    if response_format is None or not is_string(response_format["type"], "json_object"):
        return False

    if not isinstance(headers_assign, ast.Assign) or len(headers_assign.targets) != 1 or not is_name(headers_assign.targets[0], "headers"):
        return False
    headers = dict_entries(headers_assign.value, {"Authorization", "Content-Type"})
    if headers is None:
        return False
    authorization = headers["Authorization"]
    if not (
        isinstance(authorization, ast.JoinedStr) and len(authorization.values) == 2
        and is_string(authorization.values[0], "Bearer ")
        and isinstance(authorization.values[1], ast.FormattedValue)
        and is_attr(authorization.values[1].value, "settings", "cerebras_api_key")
        and authorization.values[1].format_spec is None
    ):
        return False
    if not is_string(headers["Content-Type"], "application/json"):
        return False

    if not isinstance(request_context, ast.AsyncWith) or len(request_context.items) != 1:
        return False
    item = request_context.items[0]
    client_call = item.context_expr
    if (
        not isinstance(client_call, ast.Call) or not is_attr(client_call.func, "httpx", "AsyncClient")
        or client_call.args or len(client_call.keywords) != 1
        or client_call.keywords[0].arg != "timeout" or not is_name(client_call.keywords[0].value, "timeout")
        or not is_name(item.optional_vars, "client") or len(request_context.body) != 3
    ):
        return False
    response_assign, status_check, body_assign = request_context.body
    if (
        not isinstance(response_assign, ast.Assign) or len(response_assign.targets) != 1
        or not is_name(response_assign.targets[0], "response") or not isinstance(response_assign.value, ast.Await)
        or not isinstance(response_assign.value.value, ast.Call)
        or not is_attr(response_assign.value.value.func, "client", "post")
        or len(response_assign.value.value.args) != 1 or not is_name(response_assign.value.value.args[0], "url")
        or {keyword.arg for keyword in response_assign.value.value.keywords} != {"headers", "json"}
        or not all(
            (keyword.arg == "headers" and is_name(keyword.value, "headers"))
            or (keyword.arg == "json" and is_name(keyword.value, "payload"))
            for keyword in response_assign.value.value.keywords
        )
    ):
        return False
    if (
        not isinstance(status_check, ast.Expr) or not isinstance(status_check.value, ast.Call)
        or not is_attr(status_check.value.func, "response", "raise_for_status")
        or status_check.value.args or status_check.value.keywords
    ):
        return False
    if not isinstance(body_assign, ast.Assign) or len(body_assign.targets) != 1 or not is_name(body_assign.targets[0], "body"):
        return False
    body_parser = body_assign.value
    if not (
        (isinstance(body_parser, ast.Call) and is_attr(body_parser.func, "response", "json")
         and not body_parser.args and not body_parser.keywords)
        or (isinstance(body_parser, ast.Call) and is_name(body_parser.func, "_response_json_object")
            and len(body_parser.args) == 2 and is_name(body_parser.args[0], "response")
            and is_string(body_parser.args[1], "Cerebras") and not body_parser.keywords)
    ):
        return False

    if not isinstance(extract_try, ast.Try) or extract_try.orelse or extract_try.finalbody or len(extract_try.body) != 1 or len(extract_try.handlers) != 1:
        return False
    extract_assign = extract_try.body[0]
    if not isinstance(extract_assign, ast.Assign) or len(extract_assign.targets) != 1 or not is_name(extract_assign.targets[0], "text"):
        return False
    content = extract_assign.value
    if not isinstance(content, ast.Subscript) or not is_string(content.slice, "content"):
        return False
    content = content.value
    if not isinstance(content, ast.Subscript) or not is_string(content.slice, "message"):
        return False
    content = content.value
    if not isinstance(content, ast.Subscript) or not isinstance(content.slice, ast.Constant) or content.slice.value != 0:
        return False
    content = content.value
    if not isinstance(content, ast.Subscript) or not is_string(content.slice, "choices") or not is_name(content.value, "body"):
        return False
    handler = extract_try.handlers[0]
    if not (
        isinstance(handler.type, ast.Tuple) and [n.id for n in handler.type.elts if isinstance(n, ast.Name)] == ["KeyError", "IndexError", "TypeError"]
        and len(handler.type.elts) == 3 and handler.name == "exc" and len(handler.body) == 1
        and isinstance(handler.body[0], ast.Raise) and isinstance(handler.body[0].exc, ast.Call)
        and is_name(handler.body[0].exc.func, "AIProviderError")
        and len(handler.body[0].exc.args) == 1
        and is_string(handler.body[0].exc.args[0], "Cerebras response did not contain generated text")
        and not handler.body[0].exc.keywords and is_name(handler.body[0].cause, "exc")
    ):
        return False
    return (
        isinstance(result_return, ast.Return) and isinstance(result_return.value, ast.Call)
        and is_name(result_return.value.func, "_extract_json")
        and len(result_return.value.args) == 1 and is_name(result_return.value.args[0], "text")
        and not result_return.value.keywords
    )



def _is_replaceable_legacy_market_context(
    tree: ast.Module,
    generator: ast.AsyncFunctionDef,
) -> ast.FunctionDef | None:
    """Recognize an inert-signature market binder that is only called by the replaced generator.

    Older branches commonly use a weaker market-context binder. Replace it with
    canonical context logic only when it has no decorators/default expressions and
    no caller other than the generator that is itself being replaced.
    """
    contexts = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_apply_market_context"
    ]
    if len(contexts) != 1:
        return None
    context = contexts[0]
    args = context.args
    if (
        context.decorator_list
        or args.posonlyargs
        or [arg.arg for arg in args.args] != ["spec"]
        or args.vararg
        or args.kwarg
        or [arg.arg for arg in args.kwonlyargs] != ["asset", "symbol", "timeframe"]
        or args.defaults
        or len(args.kw_defaults) != 3
        or any(not isinstance(value, ast.Constant) or value.value is not None for value in args.kw_defaults)
    ):
        return None

    def inert_annotation(node: ast.AST | None) -> bool:
        if node is None or isinstance(node, (ast.Name, ast.Constant)):
            return True
        if isinstance(node, ast.Attribute):
            return inert_annotation(node.value)
        if isinstance(node, ast.Subscript):
            return inert_annotation(node.value) and inert_annotation(node.slice)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return inert_annotation(node.left) and inert_annotation(node.right)
        if isinstance(node, (ast.Tuple, ast.List)):
            return all(inert_annotation(item) for item in node.elts)
        return False

    annotations = [
        *[arg.annotation for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]],
        context.returns,
    ]
    if any(not inert_annotation(annotation) for annotation in annotations):
        return None

    # This helper is removed from runtime use together with generate_strategy_draft.
    # Reject rebinding, deletion, reflection, and literal-name lookups anywhere in the
    # module; one direct call does not prove the helper has no dynamic/hidden callers.
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "_apply_market_context":
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                return None
        if isinstance(node, ast.Constant) and node.value == "_apply_market_context":
            return None
        if isinstance(node, (ast.Global, ast.Nonlocal)) and "_apply_market_context" in node.names:
            return None
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name) and function.id in {
                "globals", "locals", "vars", "getattr", "setattr", "delattr", "eval", "exec"
            }:
                return None

    references = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "_apply_market_context"
        and isinstance(node.ctx, ast.Load)
    ]
    if len(references) != 1 or not generator.lineno <= references[0].lineno <= generator.end_lineno:
        return None
    direct_calls = [
        node for node in ast.walk(generator)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_apply_market_context"
    ]
    if len(direct_calls) != 1:
        return None
    return context


def transform_strategy_ai(commit: str) -> None:
    path = ROOT / "app/strategy_ai.py"
    if not path.is_file():
        raise MigrationError("app/strategy_ai.py is absent")
    source = path.read_text(encoding="utf-8")
    if not PROVIDER_RE.search(source):
        return
    canonical = canonical_text(commit, "app/strategy_ai.py")
    old_tree = ast.parse(source, filename=str(path))
    postponed_annotations = _uses_postponed_annotations(old_tree)
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
    safe_legacy_flow = _is_safe_legacy_hosted_strategy_generator(legacy_generate)
    if not safe_trampoline and not safe_legacy_flow:
        raise MigrationError(
            "generate_strategy_draft contains unrecognized branch-specific behavior; refusing to overwrite it"
        )
    if safe_trampoline:
        generator_call = body[0].value.value
        if (
            len(generator_call.args) != 2
            or not isinstance(generator_call.args[0], ast.Constant)
            or generator_call.args[0].value not in {"gemini", "groq", "cerebras", "openai"}
            or not isinstance(generator_call.args[1], ast.Name)
            or generator_call.args[1].id != "prompt"
            or generator_call.keywords
        ):
            raise MigrationError(
                "generate_strategy_draft trampoline arguments may contain branch-specific input handling; refusing to discard it"
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
    )
    provider_adapter: str | None = None
    if direct_provider_trampoline:
        provider_call = provider_body[0].value.value
        provider_adapter = _provider_adapter_name(provider_call.func, old_tree)
        direct_provider_trampoline = (
            provider_adapter in provider_delegations
            and len(provider_call.args) == 1
            and isinstance(provider_call.args[0], ast.Name)
            and provider_call.args[0].id == "prompt"
            and not provider_call.keywords
        )
        if safe_legacy_flow:
            # A multi-provider fallback loop requires a dispatcher that actually
            # routes each provider name to its matching adapter.
            direct_provider_trampoline = False
        elif safe_trampoline and direct_provider_trampoline:
            adapter_by_provider = {
                "gemini": "_gemini_json",
                "groq": "_groq_json",
                "cerebras": "_cerebras_json",
                "openai": "_openai_json",
            }
            direct_provider_trampoline = (
                adapter_by_provider.get(generator_call.args[0].value)
                == provider_adapter
            )
    safe_provider_dispatcher = _is_safe_legacy_provider_dispatcher(legacy_provider, old_tree)
    if not direct_provider_trampoline and not safe_provider_dispatcher:
        raise MigrationError(
            "_generate_with_provider contains additional or unknown behavior or side-effecting arguments; refusing to discard its body"
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

    removed_candidates = {"SYSTEM_PROMPT", "_strategy_prompt", "_generate_with_provider"}

    def module_execution_loads(node: ast.AST) -> set[str]:
        loaded: set[str] = set()
        if isinstance(node, ast.AnnAssign) and postponed_annotations:
            # Do not count postponed annotation names as runtime reads.
            if node.value is not None:
                loaded.update(module_execution_loads(node.target))
                loaded.update(module_execution_loads(node.value))
            elif not isinstance(node.target, ast.Name):
                loaded.update(module_execution_loads(node.target))
            return loaded
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            executed = [
                *node.decorator_list, *node.args.defaults,
                *(value for value in node.args.kw_defaults if value is not None),
            ]
            if not postponed_annotations:
                executed.extend(
                    arg.annotation
                    for arg in [
                        *node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs
                    ]
                    if arg.annotation is not None
                )
                if node.args.vararg and node.args.vararg.annotation is not None:
                    executed.append(node.args.vararg.annotation)
                if node.args.kwarg and node.args.kwarg.annotation is not None:
                    executed.append(node.args.kwarg.annotation)
                if node.returns is not None:
                    executed.append(node.returns)
            for part in executed:
                loaded.update(module_execution_loads(part))
            return loaded
        if isinstance(node, ast.ClassDef):
            for part in [*node.decorator_list, *node.bases, *(kw.value for kw in node.keywords), *node.body]:
                loaded.update(module_execution_loads(part))
            return loaded
        if isinstance(node, ast.Lambda):
            for part in [*node.args.defaults, *(value for value in node.args.kw_defaults if value is not None)]:
                loaded.update(module_execution_loads(part))
            return loaded
        if isinstance(node, ast.If):
            loaded.update(module_execution_loads(node.test))
            condition_known = False
            try:
                condition = ast.literal_eval(node.test)
                condition_known = True
            except (ValueError, TypeError):
                condition = None
            selected = (
                node.body if bool(condition) else node.orelse
            ) if condition_known else [*node.body, *node.orelse]
            for part in selected:
                loaded.update(module_execution_loads(part))
            return loaded
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                loaded.add(node.id)
            return loaded
        for child in ast.iter_child_nodes(node):
            loaded.update(module_execution_loads(child))
        return loaded

    def module_statement_bindings(
        node: ast.AST,
        available: set[str],
        *,
        in_class: bool = False,
        class_globals: set[str] | None = None,
    ) -> set[str]:
        """Return names guaranteed available after a module-executed statement.

        Alternatives are merged by intersection, so any path that can delete a
        dependency removes it from the guaranteed set. Statements inside static
        branches are analyzed in execution order; exception targets are deleted
        when the handler exits, matching Python's runtime semantics.
        """
        visible = (lambda names: names & (class_globals or set())) if in_class else (lambda names: names)

        def sequence(
            statements: list[ast.stmt],
            initial: set[str],
            *,
            nested_class: bool = False,
            nested_globals: set[str] | None = None,
        ) -> set[str]:
            state = set(initial)
            for item in statements:
                state = module_statement_bindings(
                    item, state, in_class=nested_class, class_globals=nested_globals
                )
            return state

        def class_global_names(class_node: ast.ClassDef) -> set[str]:
            names: set[str] = set()
            pending = list(class_node.body)
            while pending:
                item = pending.pop()
                if isinstance(item, ast.Global):
                    names.update(item.names)
                    continue
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    continue
                pending.extend(ast.iter_child_nodes(item))
            return names

        if isinstance(node, ast.Delete):
            result = set(available)
            result.difference_update(
                name for target in node.targets for name in visible(_assigned_names(target))
            )
            return result

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return set(available) | visible({node.name})

        if isinstance(node, ast.ClassDef):
            globals_in_class = class_global_names(node)
            after_body = sequence(
                node.body,
                set(available),
                nested_class=True,
                nested_globals=globals_in_class,
            )
            return after_body | visible({node.name})

        if isinstance(node, ast.If):
            try:
                condition = ast.literal_eval(node.test)
                condition_known = True
            except (ValueError, TypeError):
                condition = None
                condition_known = False
            if condition_known:
                selected = node.body if bool(condition) else node.orelse
                return sequence(selected, set(available), nested_class=in_class, nested_globals=class_globals)
            left = sequence(node.body, set(available), nested_class=in_class, nested_globals=class_globals)
            right = sequence(node.orelse, set(available), nested_class=in_class, nested_globals=class_globals)
            return left & right

        if isinstance(node, (ast.Try, ast.TryStar)):
            normal = sequence(
                [*node.body, *node.orelse],
                set(available),
                nested_class=in_class,
                nested_globals=class_globals,
            )
            outcomes = [normal]
            # A handler can run after any prefix of the try body. Analyze from
            # both the incoming state and completed body state to fail closed on
            # names which an exception path can leave unbound.
            # Exceptions may occur after any completed top-level statement in
            # the try suite, not just before it starts or after the entire suite.
            exceptional_inputs: set[frozenset[str]] = {frozenset(available)}
            prefix_state = set(available)
            for try_statement in node.body:
                prefix_state = module_statement_bindings(
                    try_statement, prefix_state, in_class=in_class, class_globals=class_globals
                )
                exceptional_inputs.add(frozenset(prefix_state))
            for incoming in exceptional_inputs:
                for handler in node.handlers:
                    handled = sequence(
                        handler.body,
                        set(incoming),
                        nested_class=in_class,
                        nested_globals=class_globals,
                    )
                    if handler.name and handler.name in visible({handler.name}):
                        handled.discard(handler.name)
                    outcomes.append(handled)
            if node.finalbody:
                outcomes = [
                    sequence(
                        node.finalbody,
                        outcome,
                        nested_class=in_class,
                        nested_globals=class_globals,
                    )
                    for outcome in outcomes
                ]
            result = set.intersection(*outcomes) if outcomes else set(available)
            return result

        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            def loop_body_effect(
                statements: list[ast.stmt], initial: set[str]
            ) -> tuple[list[set[str]], list[set[str]], list[set[str]]]:
                """Return normal, break, and continue states without walking past exits."""
                active = [set(initial)]
                break_states: list[set[str]] = []
                continue_states: list[set[str]] = []
                for statement in statements:
                    next_active: list[set[str]] = []
                    for state in active:
                        if isinstance(statement, ast.Break):
                            break_states.append(state)
                            continue
                        if isinstance(statement, ast.Continue):
                            continue_states.append(state)
                            continue
                        if isinstance(statement, ast.If):
                            condition_known = False
                            try:
                                condition = ast.literal_eval(statement.test)
                                condition_known = True
                            except (ValueError, TypeError):
                                condition = None
                            if condition_known:
                                branches = [
                                    statement.body if bool(condition) else statement.orelse
                                ]
                            else:
                                branches = [statement.body, statement.orelse]
                            for branch in branches:
                                normal, broken, continued = loop_body_effect(branch, state)
                                next_active.extend(normal)
                                break_states.extend(broken)
                                continue_states.extend(continued)
                            continue
                        if isinstance(statement, (ast.Try, ast.TryStar)):
                            body_normal, body_breaks, body_continues = loop_body_effect(statement.body, state)
                            current_flows: list[tuple[str, set[str]]] = [
                                ("break", item) for item in body_breaks
                            ] + [("continue", item) for item in body_continues]
                            for normal_state in body_normal:
                                if statement.orelse:
                                    normal, broken, continued = loop_body_effect(statement.orelse, normal_state)
                                else:
                                    normal, broken, continued = [normal_state], [], []
                                current_flows.extend(("normal", item) for item in normal)
                                current_flows.extend(("break", item) for item in broken)
                                current_flows.extend(("continue", item) for item in continued)

                            # Except handlers can run after a completed statement in
                            # the try suite. Keep each prefix state, but never treat
                            # an unconditional break/continue itself as an exception.
                            handler_inputs: list[set[str]] = []
                            if statement.handlers:
                                prefixes = [set(state)]
                                prefix_active = [set(state)]
                                for try_item in statement.body:
                                    next_prefix_active: list[set[str]] = []
                                    for prefix_state in prefix_active:
                                        if not isinstance(try_item, (ast.Break, ast.Continue)):
                                            handler_inputs.append(set(prefix_state))
                                        normal, _, _ = loop_body_effect([try_item], prefix_state)
                                        next_prefix_active.extend(normal)
                                    prefix_active = next_prefix_active
                                    prefixes.extend(set(item) for item in prefix_active)
                                    if not prefix_active:
                                        break
                                handler_inputs.extend(prefixes[1:])
                                for incoming in {frozenset(item) for item in handler_inputs}:
                                    for handler in statement.handlers:
                                        normal, broken, continued = loop_body_effect(handler.body, set(incoming))
                                        handler_states = [
                                            ("normal", item) for item in normal
                                        ] + [("break", item) for item in broken] + [
                                            ("continue", item) for item in continued
                                        ]
                                        if handler.name and handler.name in visible({handler.name}):
                                            for _, item in handler_states:
                                                item.discard(handler.name)
                                        current_flows.extend(handler_states)

                            if statement.finalbody:
                                finalized: list[tuple[str, set[str]]] = []
                                for previous_flow, flow_state in current_flows:
                                    normal, broken, continued = loop_body_effect(statement.finalbody, flow_state)
                                    finalized.extend((previous_flow, item) for item in normal)
                                    finalized.extend(("break", item) for item in broken)
                                    finalized.extend(("continue", item) for item in continued)

                                # An exception that is not caught by any handler can
                                # still be converted into loop control by finally.
                                # Capture each reachable pre-statement state, so a
                                # failed import/assignment before a finally-break does
                                # not make a later binding appear guaranteed.
                                exceptional_prefixes: list[set[str]] = []
                                exceptional_active = [set(state)]
                                for try_item in statement.body:
                                    next_exceptional_active: list[set[str]] = []
                                    for prefix_state in exceptional_active:
                                        if not isinstance(try_item, (ast.Break, ast.Continue)):
                                            exceptional_prefixes.append(set(prefix_state))
                                        normal, _, _ = loop_body_effect([try_item], prefix_state)
                                        next_exceptional_active.extend(normal)
                                    exceptional_active = next_exceptional_active
                                    if not exceptional_active:
                                        break
                                exceptional_prefixes.extend(exceptional_active)
                                if statement.orelse:
                                    else_active = exceptional_active
                                    for else_item in statement.orelse:
                                        next_else_active: list[set[str]] = []
                                        for prefix_state in else_active:
                                            if not isinstance(else_item, (ast.Break, ast.Continue)):
                                                exceptional_prefixes.append(set(prefix_state))
                                            normal, _, _ = loop_body_effect([else_item], prefix_state)
                                            next_else_active.extend(normal)
                                        else_active = next_else_active
                                        if not else_active:
                                            break
                                    exceptional_prefixes.extend(else_active)

                                # Normal finalizer exits preserve the unhandled
                                # exception and therefore do not rejoin module flow.
                                # Only a finalizer break/continue makes that path
                                # a reachable loop exit.
                                for exceptional_state in exceptional_prefixes:
                                    _, broken, continued = loop_body_effect(
                                        statement.finalbody, exceptional_state
                                    )
                                    finalized.extend(("break", item) for item in broken)
                                    finalized.extend(("continue", item) for item in continued)
                                current_flows = finalized
                            next_active.extend(item for flow, item in current_flows if flow == "normal")
                            break_states.extend(item for flow, item in current_flows if flow == "break")
                            continue_states.extend(item for flow, item in current_flows if flow == "continue")
                            continue

                        if isinstance(statement, ast.With):
                            # Names assigned in the body are not automatically
                            # guaranteed: a context manager may suppress an exception
                            # raised before an assignment finishes, then execution
                            # continues after the with statement without that binding.
                            with_state = set(state)
                            normal, broken, continued = loop_body_effect(statement.body, with_state)
                            next_active.extend(normal)
                            next_active.append(set(state))
                            break_states.extend(broken)
                            continue_states.extend(continued)
                            continue

                        if isinstance(statement, ast.Match):
                            # If no case matches, execution continues unchanged.
                            next_active.append(set(state))
                            for case in statement.cases:
                                case_state = set(state) | visible(_defined_names(case.pattern))
                                normal, broken, continued = loop_body_effect(case.body, case_state)
                                next_active.extend(normal)
                                break_states.extend(broken)
                                continue_states.extend(continued)
                            continue
                        next_active.append(
                            module_statement_bindings(
                                statement, state, in_class=in_class, class_globals=class_globals
                            )
                        )
                    active = next_active
                    if not active:
                        break
                return active, break_states, continue_states

            guaranteed_nonempty_iterable = False
            if isinstance(node, (ast.For, ast.AsyncFor)):
                try:
                    iterable = ast.literal_eval(node.iter)
                except (ValueError, TypeError):
                    iterable = None
                if isinstance(iterable, (list, tuple, dict, set, str, bytes)):
                    if not iterable:
                        return sequence(node.orelse, set(available), nested_class=in_class, nested_globals=class_globals)
                    guaranteed_nonempty_iterable = True
            else:
                condition_known = False
                try:
                    condition = ast.literal_eval(node.test)
                    condition_known = True
                except (ValueError, TypeError):
                    condition = None
                if condition_known and not bool(condition):
                    return sequence(node.orelse, set(available), nested_class=in_class, nested_globals=class_globals)

            always_true_while = isinstance(node, ast.While) and condition_known and bool(condition)
            outcomes: list[set[str]] = []
            if not always_true_while and not guaranteed_nonempty_iterable:
                # Only include a zero-iteration exit when it is feasible: a
                # literal non-empty iterable is guaranteed to enter the body.
                outcomes.append(
                    sequence(node.orelse, set(available), nested_class=in_class, nested_globals=class_globals)
                )

            frontier = [set(available)]
            seen_states = {frozenset(available)}
            while frontier:
                iteration_start = frontier.pop()
                normal_states, break_states, continue_states = loop_body_effect(
                    node.body, iteration_start
                )
                outcomes.extend(break_states)  # break skips the else suite
                if not always_true_while:
                    for completed in [*normal_states, *continue_states]:
                        outcomes.append(
                            sequence(node.orelse, completed, nested_class=in_class, nested_globals=class_globals)
                        )
                for next_iteration in [*normal_states, *continue_states]:
                    marker = frozenset(next_iteration)
                    if marker not in seen_states:
                        seen_states.add(marker)
                        frontier.append(next_iteration)
            # A statically infinite loop with no reachable break never completes
            # module import. Do not claim any binding survives such a construct.
            if not outcomes:
                return set()
            return set.intersection(*outcomes)

        if isinstance(node, ast.With):
            # Module-level with bodies execute if the context is entered. Analyze
            # the body, then conservatively intersect with the untouched state
            # because __enter__ may fail before the body starts.
            body = sequence(node.body, set(available), nested_class=in_class, nested_globals=class_globals)
            return body & set(available)

        if isinstance(node, ast.Match):
            outcomes = [set(available)]
            for case in node.cases:
                outcomes.append(
                    sequence(case.body, set(available), nested_class=in_class, nested_globals=class_globals)
                )
            return set.intersection(*outcomes)

        if isinstance(node, ast.ExceptHandler):
            handled = sequence(node.body, set(available), nested_class=in_class, nested_globals=class_globals)
            if node.name and node.name in visible({node.name}):
                handled.discard(node.name)
            return handled

        if isinstance(node, ast.AnnAssign) and node.value is None:
            # Annotation-only targets do not create a runtime value binding.
            return set(available)

        names_bound = visible(_defined_names(node))
        return set(available) | names_bound


    retained_module_bindings: set[str] = set()
    for retained_node in retained_nodes:
        if isinstance(retained_node, ast.Delete):
            deleted = {
                part.id for target in retained_node.targets for part in ast.walk(target)
                if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Del)
            } & removed_candidates
            if deleted:
                raise MigrationError(
                    "retained strategy module deletes a removed global definition: " + ", ".join(sorted(deleted))
                )
        early_reads = (module_execution_loads(retained_node) & removed_candidates) - retained_module_bindings
        if early_reads:
            raise MigrationError(
                "retained module code reads removed global(s) before a retained binding is available: "
                + ", ".join(sorted(early_reads))
            )
        retained_module_bindings = module_statement_bindings(retained_node, retained_module_bindings)

    removed_local_dependencies = removed_candidates - retained_module_bindings
    retained_module = ast.Module(body=retained_nodes, type_ignores=[])
    try:
        retained_symbols = symtable.symtable(ast.unparse(retained_module), str(path), "exec")
    except (SyntaxError, ValueError) as exc:
        raise MigrationError("unable to resolve retained strategy helper scopes safely") from exc
    retained_removed_global_uses: set[str] = set()

    def collect_removed_global_references(table: symtable.SymbolTable) -> None:
        for removed_name in removed_local_dependencies:
            try:
                symbol = table.lookup(removed_name)
            except KeyError:
                continue
            if symbol.is_referenced() and symbol.is_global():
                retained_removed_global_uses.add(removed_name)
        for child in table.get_children():
            collect_removed_global_references(child)

    collect_removed_global_references(retained_symbols)
    if retained_removed_global_uses:
        raise MigrationError(
            "retained strategy helper depends on removed local definition(s) "
            + ", ".join(sorted(retained_removed_global_uses))
            + "; refusing to remove prompt or provider behavior"
        )

    for retained_node in retained_nodes:
        targets = (
            retained_node.targets if isinstance(retained_node, ast.Assign)
            else [retained_node.target] if isinstance(retained_node, ast.AnnAssign)
            else []
        )
        if any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
            try:
                exports = ast.literal_eval(retained_node.value) if retained_node.value is not None else None
            except (ValueError, TypeError):
                exports = None
            if not isinstance(exports, (list, tuple, set)) or not all(isinstance(item, str) for item in exports):
                raise MigrationError("strategy module has dynamic __all__; refusing to remove names that may be exported")
            lost = sorted(removed_local_dependencies & set(exports))
            if lost:
                raise MigrationError("strategy module __all__ exports definitions removed by migration: " + ", ".join(lost))
        if isinstance(retained_node, ast.AugAssign) and isinstance(retained_node.target, ast.Name) and retained_node.target.id == "__all__":
            if not isinstance(retained_node.op, ast.Add):
                raise MigrationError("strategy module mutates __all__ dynamically; refusing to remove potentially exported names")
            try:
                values = ast.literal_eval(retained_node.value)
            except (ValueError, TypeError):
                values = None
            if not isinstance(values, (list, tuple, set)) or not all(isinstance(item, str) for item in values):
                raise MigrationError("strategy module mutates __all__ dynamically; refusing to remove potentially exported names")
            lost = sorted(removed_local_dependencies & set(values))
            if lost:
                raise MigrationError("strategy module __all__ exports definitions removed by migration: " + ", ".join(lost))
        if isinstance(retained_node, ast.Expr) and isinstance(retained_node.value, ast.Call):
            call = retained_node.value
            if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) and call.func.value.id == "__all__":
                if call.func.attr not in {"append", "extend", "insert"}:
                    raise MigrationError("strategy module mutates __all__ dynamically; refusing to remove potentially exported names")
                try:
                    values = [ast.literal_eval(arg) for arg in call.args]
                except (ValueError, TypeError):
                    values = []
                flattened: set[str] = set()
                for value in values:
                    if isinstance(value, str):
                        flattened.add(value)
                    elif isinstance(value, (list, tuple, set)) and all(isinstance(item, str) for item in value):
                        flattened.update(value)
                    else:
                        raise MigrationError("strategy module mutates __all__ dynamically; refusing to remove potentially exported names")
                lost = sorted(removed_local_dependencies & flattened)
                if lost:
                    raise MigrationError("strategy module __all__ exports definitions removed by migration: " + ", ".join(lost))

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
    # Verify that every global read in the transplanted generator is still available.
    local_names = {canonical_generate.name}
    local_names.update(arg.arg for arg in ast.walk(canonical_generate.args) if isinstance(arg, ast.arg))
    for part in ast.walk(canonical_generate):
        if isinstance(part, ast.Name) and isinstance(part.ctx, (ast.Store, ast.Del)):
            local_names.add(part.id)
        elif isinstance(part, ast.ExceptHandler) and part.name:
            local_names.add(part.name)
        elif isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and part is not canonical_generate:
            local_names.add(part.name)
    canonical_global_loads = {
        part.id for part in ast.walk(canonical_generate)
        if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Load)
    } - local_names - set(dir(builtins))
    available_names: set[str] = set()
    for retained_node in retained_nodes:
        available_names = module_statement_bindings(retained_node, available_names)
    remappable_names = {"NativeAIError", "compose_strategy_spec"}
    missing_dependencies = sorted(canonical_global_loads - available_names - remappable_names)
    if missing_dependencies:
        raise MigrationError(
            "canonical strategy generator depends on missing module binding(s): "
            + ", ".join(missing_dependencies)
            + "; refusing to publish a broken strategy module"
        )

    if not _is_compatible_strategy_schema_and_context(old_tree, new_tree):
        # Older branches commonly have a safe market-context helper that predates
        # canonical timeframe normalization. Replace that helper—not its schema—
        # with the canonical implementation, then rerun the full schema/global-binding
        # proof before rewriting any source.
        legacy_context = _is_replaceable_legacy_market_context(old_tree, legacy_generate)
        canonical_contexts = [
            node for node in new_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_apply_market_context"
        ]
        if legacy_context is not None and len(canonical_contexts) == 1:
            source_lines_for_context = source.splitlines(keepends=True)
            canonical_lines_for_context = canonical.splitlines(keepends=True)
            canonical_context = canonical_contexts[0]
            replacement_lines = canonical_lines_for_context[
                canonical_context.lineno - 1:canonical_context.end_lineno
            ]
            source = "".join(
                source_lines_for_context[:legacy_context.lineno - 1]
                + replacement_lines
                + source_lines_for_context[legacy_context.end_lineno:]
            )
            old_tree = ast.parse(source, filename=str(path))
        if not _is_compatible_strategy_schema_and_context(old_tree, new_tree):
            raise MigrationError(
                "strategy schema/context helpers do not satisfy the canonical Native AI draft contract; refusing to replace the hosted generator"
            )

    # Context replacement changes source line positions. Always derive import nodes
    # from the current reparsed tree rather than retaining pre-replacement AST locations.
    ai_provider_imports = [
        node for node in old_tree.body
        if isinstance(node, ast.ImportFrom)
        and node.level == 1
        and node.module == "ai_providers"
    ]

    edits: list[tuple[int, int, str]] = []
    found_generate = False
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
    if RETIRED_PROVIDER_RE.search(result):
        raise MigrationError("strategy module still contains retired-provider references")
    ast.parse(result, filename=str(path))
    path.write_text(result, encoding="utf-8")


def _ensure_native_research_import(source: str, filename: str) -> str:
    tree = ast.parse(source, filename=filename)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "native_ai":
            if any(alias.name == "native_research_review" and (alias.asname is None or alias.asname == alias.name)
                   for alias in node.names):
                return source
    prefix_end = 0
    for index, node in enumerate(tree.body):
        if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            prefix_end = node.end_lineno
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            prefix_end = node.end_lineno
            continue
        break
    return _apply_line_edits(source, [
        (prefix_end + 1, prefix_end, "from .native_ai import native_research_review\n")
    ])


def _ensure_groq_research_import(source: str, filename: str) -> str:
    tree = ast.parse(source, filename=filename)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "groq_ai":
            if any(alias.name == "groq_research_review" and (alias.asname is None or alias.asname == alias.name)
                   for alias in node.names):
                return source
    prefix_end = 0
    for index, node in enumerate(tree.body):
        if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            prefix_end = node.end_lineno
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            prefix_end = node.end_lineno
            continue
        break
    return _apply_line_edits(source, [
        (prefix_end + 1, prefix_end, "from .groq_ai import groq_research_review\n")
    ])


def _ensure_groq_review_persisted(source: str, filename: str) -> str:
    """Repair only ResearchRun.review_json projections; ignore unrelated JSON diagnostics."""
    tree = ast.parse(source, filename=filename)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def is_research_run_projection(call: ast.Call) -> bool:
        parent = parents.get(call)
        while parent is not None:
            if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id == "ResearchRun":
                return any(keyword.arg == "review_json" for keyword in parent.keywords)
            parent = parents.get(parent)
        return False

    insertions: list[tuple[int, str]] = []
    complete_projection = False
    projection_seen = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not is_research_run_projection(node):
            continue
        if not (
            isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "json" and node.func.attr == "dumps" and node.args
        ):
            continue
        projection = node.args[0]
        if isinstance(projection, ast.Name) and projection.id == "review":
            complete_projection = True
            continue
        if isinstance(projection, ast.Dict):
            keys = projection.keys
            if not all(isinstance(key, ast.Constant) and isinstance(key.value, str) for key in keys):
                raise MigrationError(f"{filename} has a dynamic ResearchRun.review_json mapping; refusing to omit Groq review history")
            fields = [key.value for key in keys]
            if "groq_research_review" in fields:
                continue
            if not any(name in fields for name in ("ai_brain_mode", "paper_candidates", "native_ai_review", "external_ai_review")):
                continue
            projection_seen = True
            source_lines = source.splitlines(keepends=True)
            line_bytes = source_lines[projection.end_lineno - 1].encode("utf-8")
            try:
                column = len(line_bytes[:projection.end_col_offset].decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise MigrationError(f"{filename} review mapping has an invalid source position") from exc
            offset = sum(len(line) for line in source_lines[:projection.end_lineno - 1]) + column
            if offset < 1 or source[offset - 1:offset] != "}":
                raise MigrationError(f"{filename} has an unexpected ResearchRun.review_json mapping")
            last_value = projection.values[-1] if projection.values else None
            trailing_segment = ""
            if last_value is not None:
                end_line = source_lines[last_value.end_lineno - 1]
                end_column = len(
                    end_line.encode("utf-8")[:last_value.end_col_offset].decode("utf-8")
                )
                last_offset = sum(len(line) for line in source_lines[:last_value.end_lineno - 1]) + end_column
                trailing_segment = source[last_offset:offset - 1]
            trailing_segment = re.sub(r"#[^\n]*", "", trailing_segment)
            addition = (
                '"groq_research_review": review.get("groq_research_review")'
                if "," in trailing_segment
                else ', "groq_research_review": review.get("groq_research_review")'
            )
            insertions.append((offset - 1, addition))
            continue
        if not isinstance(projection, ast.DictComp):
            continue
        if not (
            isinstance(projection.key, ast.Name)
            and isinstance(projection.value, ast.Call)
            and isinstance(projection.value.func, ast.Attribute)
            and isinstance(projection.value.func.value, ast.Name)
            and projection.value.func.value.id == "review"
            and projection.value.func.attr == "get"
            and len(projection.value.args) == 1
            and isinstance(projection.value.args[0], ast.Name)
            and projection.value.args[0].id == projection.key.id
            and len(projection.generators) == 1
            and isinstance(projection.generators[0].target, ast.Name)
            and projection.generators[0].target.id == projection.key.id
            and isinstance(projection.generators[0].iter, ast.Tuple)
        ):
            continue
        field_tuple = projection.generators[0].iter
        if not all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in field_tuple.elts):
            continue
        projection_seen = True
        fields = [item.value for item in field_tuple.elts]
        if "groq_research_review" in fields:
            continue
        if not any(name in fields for name in ("ai_brain_mode", "paper_candidates", "native_ai_review", "external_ai_review")):
            continue
        source_lines = source.splitlines(keepends=True)
        line_bytes = source_lines[field_tuple.end_lineno - 1].encode("utf-8")
        try:
            column = len(line_bytes[:field_tuple.end_col_offset].decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise MigrationError(f"{filename} review projection has an invalid source position") from exc
        offset = sum(len(line) for line in source_lines[:field_tuple.end_lineno - 1]) + column
        if offset < 1 or source[offset - 1:offset] != ")":
            raise MigrationError(f"{filename} has an unexpected ResearchRun.review_json projection")
        last_element = field_tuple.elts[-1] if field_tuple.elts else None
        trailing_segment = ""
        if last_element is not None:
            end_line = source_lines[last_element.end_lineno - 1]
            end_column = len(
                end_line.encode("utf-8")[:last_element.end_col_offset].decode("utf-8")
            )
            last_offset = sum(len(line) for line in source_lines[:last_element.end_lineno - 1]) + end_column
            trailing_segment = source[last_offset:offset - 1]
        trailing_segment = re.sub(r"#[^\n]*", "", trailing_segment)
        addition = '"groq_research_review"' if "," in trailing_segment else ', "groq_research_review"'
        insertions.append((offset - 1, addition))

    if complete_projection and not projection_seen:
        return source
    for offset, addition in sorted(set(insertions), reverse=True):
        source = source[:offset] + addition + source[offset:]
    ast.parse(source, filename=filename)
    return source


def _ensure_groq_research_call(source: str, filename: str) -> str:
    """Repair already-Native-AI branches or refuse an unrecognized async code shape."""
    tree = ast.parse(source, filename=filename)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def enclosing_function(node: ast.AST) -> ast.AST | None:
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return parent
            parent = parents.get(parent)
        return None

    native_assignment = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(
            isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
            and target.value.id == "review" and isinstance(target.slice, ast.Constant)
            and target.slice.value == "native_ai_review"
            for target in targets
        ):
            native_assignment = node
            break
    if native_assignment is None:
        return source

    owner = enclosing_function(native_assignment)
    if not isinstance(owner, ast.AsyncFunctionDef):
        raise MigrationError(f"{filename} has a Native AI review outside an async function; refusing an unsafe Groq await")
    owner_names = [node for node in ast.walk(owner) if isinstance(node, ast.Name)]
    if not any(node.id == "research" for node in owner_names) or not any(node.id == "intelligence" for node in owner_names):
        raise MigrationError(f"{filename} lacks the recognized research/intelligence scope for the optional Groq reviewer")
    for node in ast.walk(owner):
        if isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id == "groq_research_review")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "groq_research_review")
        ):
            return _ensure_native_research_import(_ensure_groq_research_import(source, filename), filename)

    lines = source.splitlines(keepends=True)
    current = lines[native_assignment.lineno - 1]
    indent = current[:len(current) - len(current.lstrip())]
    lines.insert(
        native_assignment.end_lineno,
        indent + 'review["groq_research_review"] = await groq_research_review(research, intelligence)\n',
    )
    return _ensure_native_research_import(_ensure_groq_research_import("".join(lines), filename), filename)


def transform_daily_research() -> None:
    path = ROOT / "app/daily_research.py"
    if not path.is_file():
        raise MigrationError("app/daily_research.py is absent")
    original = path.read_text(encoding="utf-8")
    source = _ensure_groq_review_persisted(original, str(path))
    # Repair already-Native-AI branches too; they must call and persist the optional
    # reviewer rather than merely receive an unused module during propagation.
    legacy_daily_provider_re = re.compile(
        r"(?i)(?:dual_ai_research_review|_gemini_json|_groq_json|_cerebras_json|"
        r"gemini_api_key|groq_api_key|cerebras_api_key|ai_gateway_)"
    )
    if not legacy_daily_provider_re.search(source):
        source = _ensure_groq_research_call(source, str(path))
        source = _ensure_groq_review_persisted(source, str(path))
        if source != original:
            ast.parse(source, filename=str(path))
            path.write_text(source, encoding="utf-8")
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
        if any(isinstance(part, ast.Call) for part in ast.walk(node.test)):
            raise MigrationError(
                "daily-research provider condition includes a call expression; refusing to discard condition side effects"
            )
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

            # Historical provider facades use either the minimal status marker or
            # this exact inert payload. Accept both, but do not erase dynamic values,
            # unknown fields, or sibling audit assignments from a legacy branch.
            value = statement.value
            if not isinstance(value, ast.Dict) or len(value.keys) != len(value.values):
                return False
            fields: dict[str, ast.AST] = {}
            for key, item in zip(value.keys, value.values):
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    return False
                if key.value in fields:
                    return False
                fields[key.value] = item

            status = fields.get("status")
            if not (
                isinstance(status, ast.Constant)
                and status.value == "NOT_CONFIGURED"
            ):
                return False
            if set(fields) == {"status"}:
                return True
            if set(fields) != {"status", "providers", "execution_authority"}:
                return False
            providers = fields["providers"]
            authority = fields["execution_authority"]
            return (
                isinstance(providers, ast.Dict)
                and not providers.keys
                and not providers.values
                and isinstance(authority, ast.Constant)
                and authority.value is False
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
            indent + 'review["groq_research_review"] = await groq_research_review(research, intelligence)',
        )) + "\n"
        edits.append((node.lineno, node.end_lineno, replacement))
        found_callsite = True
        break
    for imported in tree.body:
        if not (isinstance(imported, ast.ImportFrom) and imported.level == 1 and imported.module == "ai_providers"):
            continue
        if not any(alias.name in provider_delegations for alias in imported.names):
            continue
        retained_aliases = [alias for alias in imported.names if alias.name not in provider_delegations]
        replacement_lines = [
            "from .native_ai import native_research_review\n",
            "from .groq_ai import groq_research_review\n",
        ]
        if retained_aliases:
            retained = ", ".join(alias.name + (f" as {alias.asname}" if alias.asname else "") for alias in retained_aliases)
            replacement_lines.append(f"from .ai_providers import {retained}\n")
        edits.append((imported.lineno, imported.end_lineno, "".join(replacement_lines)))
    if not found_callsite:
        raise MigrationError("daily-research provider callsite is unexpected; refusing lossy rewrite")
    result = _apply_line_edits(source, edits)
    result = _ensure_groq_research_call(result, str(path))
    result = _ensure_groq_review_persisted(result, str(path))
    if RETIRED_PROVIDER_RE.search(result):
        raise MigrationError("daily research still contains retired-provider references")
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
    # Groq remains a supported research-only reviewer. Preserve pure Groq secret/config
    # conditionals verbatim, but fail closed if they wrap unrelated deployment commands.
    source_lines = text.splitlines(keepends=True)
    groq_condition = re.compile(
        r"(?i)\b(?:SECRET_GROQ[A-Z0-9_]*|GROQ_API_KEY|GROQ_SECRET_ARG|GROQ_SECRET_ARGS|"
        r"GROQ_MODEL|GROQ_MAX_COMPLETION_TOKENS|GROQ_RESEARCH_REVIEW_ENABLED)\b"
    )
    def safe_groq_statement(statement: str) -> bool:
        stripped = statement.strip()
        if not stripped or stripped.startswith("#"):
            return True
        if re.fullmatch(r"(?i)(?:then|else|fi)", stripped):
            return True
        if re.fullmatch(r"(?i)elif\s+\[\[.*\]\]\s*;?\s*then", stripped):
            return True
        groq_guard = re.compile(
            r"^:\s*['\"]?" + re.escape("$") + r"\{(?:GROQ_API_KEY|SECRET_GROQ[A-Z0-9_]*)(?::\?[^}]*)?\}['\"]?$",
            re.I,
        )
        if groq_guard.fullmatch(stripped):
            return True
        if re.fullmatch(r'(?i)create_secret\s+atlas-groq-api-key\s+"\$GROQ_API_KEY"', stripped):
            return True
        if re.fullmatch(r"(?i)exit\s+[0-9]+", stripped):
            return True
        if re.match(r"(?i)^(?:echo|printf)\b", stripped):
            normalized = re.sub(r"\s+>&2\s*$", "", stripped)
            return not re.search(r"[&;|<>`]|\$\(", normalized)
        return re.fullmatch(
            r"(?i)(?:export\s+)?(?:SECRET_GROQ[A-Z0-9_]*|GROQ_API_KEY|GROQ_SECRET_ARG|GROQ_SECRET_ARGS|"
            r"GROQ_MODEL|GROQ_MAX_COMPLETION_TOKENS|GROQ_RESEARCH_REVIEW_ENABLED|AI_SECRET_ARGS)\s*=\s*[^;|<>]*",
            stripped,
        ) is not None

    def inert_provider_message(statement: str) -> bool:
        stripped = statement.strip()
        if not re.match(r"^\s*(?:echo|printf)\b", stripped, re.I):
            return False
        # Bash printf -v assigns a variable; it is not output-only. Reject it
        # before classifying any provider-bearing literal as removable commentary.
        if re.match(r"^\s*printf\s+-v(?:\s|$)", stripped, re.I):
            return False
        if not RETIRED_PROVIDER_RE.search(stripped):
            return False
        # Only discard literal output text. Never rewrite dynamic expansions,
        # command substitutions, redirections, pipes, or composed commands.
        normalized = re.sub(r"\s+>&2\s*$", "", stripped)
        if re.search(r"\$|\x60", normalized):
            return False
        quote = None
        escaped = False
        for character in normalized:
            if escaped:
                escaped = False
                continue
            if character == "\\" and quote != "'":
                escaped = True
                continue
            if quote is not None:
                if character == quote:
                    quote = None
                continue
            if character in {"'", '"'}:
                quote = character
                continue
            if character in ";&|<>":
                return False
        return quote is None and not escaped

    block_start = 0
    while block_start < len(source_lines):
        source_line = source_lines[block_start]
        if re.match(r"^\s*if\b", source_line, re.I) and groq_condition.search(source_line):
            inline = re.match(r"^\s*if\b.*?\bthen\s*(.*?);\s*fi\s*$", source_line, re.I)
            header = re.search(r"\bthen\b(.*)$", source_line, re.I)
            if header and not inline:
                tail = header.group(1).strip().rstrip(";").strip()
                if tail and not safe_groq_statement(tail):
                    raise MigrationError(f"{path} has an executable command on a Groq if-header; refusing it")
            if inline:
                inline_commands = [chunk.strip() for chunk in inline.group(1).split(";") if chunk.strip()]
                if any(not safe_groq_statement(command) for command in inline_commands):
                    raise MigrationError(
                        f"{path} has a mixed provider-conditioned shell block; refusing unrelated inline command"
                    )
                block_start += 1
                continue
            indent = len(source_line) - len(source_line.lstrip())
            end = block_start + 1
            while end < len(source_lines):
                if re.match(r"^\s*fi\b", source_lines[end], re.I):
                    break
                if source_lines[end].strip() and len(source_lines[end]) - len(source_lines[end].lstrip()) < indent:
                    break
                end += 1
            if end >= len(source_lines) or not re.match(r"^\s*fi\b", source_lines[end], re.I):
                raise MigrationError(f"{path} has an unterminated Groq-conditioned block")
            for block_line in source_lines[block_start + 1:end]:
                if not block_line.strip() or block_line.lstrip().startswith("#"):
                    continue
                if not safe_groq_statement(block_line):
                    raise MigrationError(
                        f"{path} has a mixed provider-conditioned shell block; refusing to discard unrelated statement: {block_line.strip()[:100]}"
                    )
            block_start = end + 1
            continue
        block_start += 1

    variable_pattern = re.compile(
        r"(?i)\b(?:SECRET_GEMINI[A-Z0-9_]*|"
        r"GEMINI_API_KEY|GROQ_STRATEGY_MODEL|GROQ_STRATEGY_API_KEY|AI_SECRET_ARGS|"
        r"CEREBRAS_[A-Z0-9_]*|AI_GATEWAY_[A-Z0-9_]*|OPENAI_[A-Z0-9_]*|OPENAI_SECRET_ARG)\b"
    )
    def split_shell_list(value: str, delimiter: str) -> list[str]:
        """Split list entries without treating delimiters inside ${...} as separators."""
        parts: list[str] = []
        start = 0
        expansion_depth = 0
        index = 0
        while index < len(value):
            if value.startswith("${", index):
                expansion_depth += 1
                index += 2
                continue
            char = value[index]
            if char == "}" and expansion_depth:
                expansion_depth -= 1
            elif char == delimiter and expansion_depth == 0:
                parts.append(value[start:index])
                start = index + 1
            index += 1
        parts.append(value[start:])
        return parts

    def scrub_key_value_list(value: str, *, preserve_groq_tokens: bool = False) -> str:
        prefix = ""
        delimiter = ","
        if value.startswith("^@^"):
            prefix, value, delimiter = "^@^", value[3:], "@"
        retired_assignment = re.compile(
            r"(?i)^(?:SECRET_GEMINI[A-Z0-9_]*|GEMINI_API_KEY|GEMINI_MODEL|GEMINI_STRATEGY_MODEL|"
            r"CEREBRAS_[A-Z0-9_]*|AI_GATEWAY_[A-Z0-9_]*|OPENAI_[A-Z0-9_]*|OPENAI_SECRET_ARG|"
            r"GROQ_STRATEGY_MODEL|GROQ_STRATEGY_API_KEY)$"
        )
        dynamic_pattern = re.escape("$") + r"\{(AI_SECRET_ARGS|GROQ_SECRET_ARG|GROQ_SECRET_ARGS)\}"
        retained: list[str] = []
        for raw_entry in split_shell_list(value, delimiter):
            entry = raw_entry.strip()
            if not entry:
                continue
            dynamic = re.fullmatch(dynamic_pattern, entry)
            if dynamic:
                if preserve_groq_tokens:
                    # Keep shared expansions in their original list position.
                    retained.append("$" + "{" + dynamic.group(1) + "}")
                continue
            key = entry.split("=", 1)[0].strip()
            if retired_assignment.fullmatch(key):
                if preserve_groq_tokens:
                    # Preserve any shared expansion carried by the retired key's value.
                    retained.extend("$" + "{" + token + "}" for token in re.findall(dynamic_pattern, entry))
                continue
            if RETIRED_PROVIDER_RE.search(key):
                raise MigrationError(f"{path} has an unrecognized provider key in a deployment list: {key}")
            if (
                preserve_groq_tokens
                and "${AI_SECRET_ARGS}" in entry
                and "${AI_SECRET_ARGS:+,${AI_SECRET_ARGS}}" not in entry
                and "${AI_SECRET_ARGS}${AI_SECRET_ARGS:+,}" not in entry
            ):
                # Normalize only a bare shared expansion attached to an item.
                # Already-conditional list expressions and incremental assignments
                # are valid shell syntax and must remain unchanged (idempotence).
                entry = entry.replace(
                    "${AI_SECRET_ARGS}",
                    "${AI_SECRET_ARGS:+,${AI_SECRET_ARGS}}",
                )
            retained.append(entry)
        return prefix + delimiter.join(retained)

    def scrub_cloud_run_flag(line: str, flag: str, *, preserve_groq_tokens: bool = False) -> str:
        match = re.search(r"(" + re.escape(flag) + r"(?:\s*=\s*|\s+))" + r"""(['"])(.*?)\2""", line)
        if not match:
            return line
        cleaned = scrub_key_value_list(match.group(3), preserve_groq_tokens=preserve_groq_tokens)
        if preserve_groq_tokens:
            # AI_SECRET_ARGS is normalized without a leading comma, whereas the
            # legacy GROQ_SECRET_ARG(S) fragments already include their comma.
            def conditional_shared_expansion(match: re.Match[str]) -> str:
                name = match.group(1)
                if name == "AI_SECRET_ARGS":
                    return "${AI_SECRET_ARGS:+,${AI_SECRET_ARGS}}"
                return "${" + name + "}"

            cleaned = re.sub(
                r"(?<!\+),\$\{(AI_SECRET_ARGS|GROQ_SECRET_ARG|GROQ_SECRET_ARGS)\}",
                conditional_shared_expansion,
                cleaned,
            )
        if not cleaned:
            return line[:match.start()] + line[match.end():]
        return line[:match.start(3)] + cleaned + line[match.end(3):]

    def scrub_ai_secret_args(line: str) -> str:
        match = re.match(r"""^(\s*(?:export\s+)?AI_SECRET_ARGS\s*=\s*)(["'])(.*)(\2\s*(?:\r?\n)?)$""", line)
        if not match:
            raise MigrationError(f"{path} has an unparseable shared AI_SECRET_ARGS assignment")
        prefix, quote, value, suffix = match.groups()
        cleaned = scrub_key_value_list(value.lstrip(","), preserve_groq_tokens=True)
        # Incremental lists must not introduce a leading/empty item when their first
        # optional Groq secret is the only surviving entry. Use the existing list
        # only when non-empty, inserting its separator conditionally.
        shared = "${AI_SECRET_ARGS}"
        if cleaned.startswith(shared + ","):
            cleaned = shared + "${AI_SECRET_ARGS:+,}" + cleaned[len(shared) + 1:]
        return prefix + quote + cleaned + suffix

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
            if re.match(r"^\s*(?:export\s+)?AI_SECRET_ARGS\s*=", line, re.I):
                output.append(scrub_ai_secret_args(line))
                continue
            provider_assignment = re.match(
                r"^\s*(?:export\s+)?(?:SECRET_GEMINI[A-Z0-9_]*|"
                r"GEMINI_API_KEY|GROQ_STRATEGY_MODEL|GROQ_STRATEGY_API_KEY|"
                r"CEREBRAS_[A-Z0-9_]*|AI_GATEWAY_[A-Z0-9_]*|OPENAI_[A-Z0-9_]*|OPENAI_SECRET_ARG)\s*=",
                line, re.I,
            )
            warning_only = inert_provider_message(stripped) or (
                re.match(r"^\s*(?:echo|printf)\b", stripped, re.I)
                and not re.match(r"^\s*printf\s+-v(?:\s|$)", stripped, re.I)
                and RETIRED_PROVIDER_RE.search(stripped)
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
        if re.match(r"^\s*(?::\s*)?['\"]?\$\{?(?:SECRET_GEMINI|GEMINI_API_KEY)", line, re.I):
            continue
        if re.match(
            r"^\s*(?:export\s+)?(?:SECRET_GEMINI[A-Z0-9_]*|"
            r"GEMINI_API_KEY|GROQ_STRATEGY_MODEL|GROQ_STRATEGY_API_KEY|"
            r"CEREBRAS_API_KEY|AI_GATEWAY_API_KEY)\s*=",
            line, re.I,
        ):
            continue
        if re.search(r"(?i)\b(?:create_secret|grant_secret_access)\s+atlas-(?:gemini|cerebras|ai-gateway|openai)\b", line):
            continue
        if stripped.startswith("#") and RETIRED_PROVIDER_RE.search(line):
            continue

        if re.match(r"^\s*(?:export\s+)?AI_SECRET_ARGS\s*=", line, re.I):
            output.append(scrub_ai_secret_args(line))
            continue

        # Remove retired pairs and normalize commas after deletion, while retaining Groq expansions.
        if re.search(r"\bENV_VARS\s*=", line):
            env_match = re.search(r"""(ENV_VARS\s*=\s*)(['"])(.*?)\2""", line)
            if env_match:
                cleaned_list = scrub_key_value_list(env_match.group(3))
                line = line[:env_match.start(3)] + cleaned_list + line[env_match.end(3):]
        if "--set-env-vars" in line:
            line = scrub_cloud_run_flag(line, "--set-env-vars")
        if "--set-secrets" in line:
            line = scrub_cloud_run_flag(line, "--set-secrets", preserve_groq_tokens=True)
        # Provider secret names in bootstrap allowlists are independent list items.
        if "for secret in" in line:
            line = re.sub(
                r"\s+atlas-(?:gemini|cerebras|ai-gateway|openai)[-a-z0-9]*",
                "",
                line,
                flags=re.I,
            )
        # Strip any remaining provider-only executable line. If a mixed executable line
        # still contains a provider token, refuse the branch rather than damage its command.
        if RETIRED_PROVIDER_RE.search(line):
            if stripped.startswith("#"):
                continue
            warning_only = inert_provider_message(stripped) or (
                re.match(r"^\s*(?:echo|printf)\b", stripped, re.I)
                and not re.match(r"^\s*printf\s+-v(?:\s|$)", stripped, re.I)
                and re.search(r"(?i)\b(?:gemini|cerebras|ai.?gateway|openai)\b", stripped)
                and re.search(r"(?i)\b(?:warn(?:ing)?|not configured|missing|unset|skipping|optional)\b", stripped)
            )
            if warning_only:
                continue
            inline_if = re.match(r"^\s*if\b.*\bthen\b(.*);\s*fi\s*$", line, re.I)
            if inline_if:
                commands = [part.strip() for part in inline_if.group(1).split(";") if part.strip()]
                safe_inline = all(
                    re.match(
                        r"^(?::|(?:export\s+)?(?:SECRET_GEMINI[A-Z0-9_]*|"
                        r"GEMINI_API_KEY|AI_SECRET_ARGS|"
                        r"CEREBRAS_[A-Z0-9_]*|AI_GATEWAY_[A-Z0-9_]*|OPENAI_[A-Z0-9_]*|OPENAI_SECRET_ARG)\s*=)",
                        command, re.I,
                    )
                    or (
                        re.match(r"^(?:echo|printf)\b", command, re.I)
                        and not re.match(r"^printf\s+-v(?:\s|$)", command, re.I)
                        and re.search(r"(?i)\b(?:gemini|cerebras|ai.?gateway|openai)\b", command)
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
    if RETIRED_PROVIDER_RE.search(cleaned):
        raise MigrationError(f"{path} still contains retired-provider wiring after shell cleanup")
    return cleaned


def _workflow_has_active_provider_wiring(text: str) -> bool:
    """Check workflow semantics while permitting inert names and protected environments."""
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^\s*(?:-\s+)?([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", line)
        if match:
            key, value = match.groups()
            if key.lower() in {"environment", "name", "run-name", "description"}:
                # Environment names can intentionally select protected approval gates;
                # labels/descriptions are not executable provider configuration.
                continue
            if PROVIDER_SETTING_NAME_RE.fullmatch(key) or RETIRED_PROVIDER_FIELD_RE.search(key):
                return True
            # Remove YAML comments before considering scalar values; quoted values
            # remain conservatively checked when the provider token is in the value.
            value = value.split(" #", 1)[0]
            if RETIRED_PROVIDER_RE.search(value) or ATLAS_PROVIDER_SECRET_RE.search(value):
                return True
            continue
        if RETIRED_PROVIDER_RE.search(line) or ATLAS_PROVIDER_SECRET_RE.search(line):
            return True
    return False


def scrub_workflow_ancillary(path: str, text: str) -> str:
    """Remove provider configuration from workflows without rewriting environment gates or commands.

    Provider env keys are removed as whole YAML entries. Shell run blocks go through the
    strict shell scrubber, which refuses mixed commands; it never strips a provider URL
    from an otherwise unrelated executable line.
    """
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            index += 1
            continue

        run_match = re.match(r"^([ \t]*)(-[ \t]+)?run:[ \t]*(.*?)[ \t]*(?:\r?\n)?$", line)
        if run_match:
            indent_text, list_marker, value = run_match.groups()
            indent = len(indent_text)
            value = value.strip()
            block_indicator = re.sub(r"\s+#.*$", "", value).strip()
            if block_indicator in {"|", "|-", "|+", ">", ">-"} or not value:
                end = index + 1
                while end < len(lines):
                    candidate = lines[end]
                    if not candidate.strip():
                        end += 1
                        continue
                    candidate_indent = len(candidate) - len(candidate.lstrip())
                    if candidate_indent <= indent:
                        break
                    end += 1
                block = lines[index + 1:end]
                nonblank_indents = [
                    len(item) - len(item.lstrip()) for item in block if item.strip()
                ]
                body_indent = min(nonblank_indents) if nonblank_indents else indent + 2
                body = "".join(
                    (item[body_indent:] if item.strip() else "")
                    for item in block
                )
                if RETIRED_PROVIDER_RE.search(body) or ATLAS_PROVIDER_SECRET_RE.search(body):
                    cleaned = scrub_shell_deployment(path, body)
                    if not cleaned.strip():
                        raise MigrationError(
                            f"{path} contains a provider-only run block; refusing to leave an invalid workflow step"
                        )
                    kept.append(line)
                    for item in cleaned.splitlines(keepends=True):
                        kept.append((" " * body_indent + item) if item.strip() else item)
                else:
                    kept.extend(lines[index:end])
                index = end
                continue
            if RETIRED_PROVIDER_RE.search(value) or ATLAS_PROVIDER_SECRET_RE.search(value):
                cleaned = scrub_shell_deployment(path, value)
                if not cleaned.strip():
                    raise MigrationError(
                        f"{path} contains a provider-only inline run; refusing to leave an invalid workflow step"
                    )
                newline = "\n" if line.endswith("\n") else ""
                kept.append(f"{indent_text}{list_marker or ''}run: {cleaned.strip()}{newline}")
                index += 1
                continue

        # GitHub Actions sequence-item labels are inert metadata just like mapping labels.
        # Preserve them verbatim even when their display text names a retired provider.
        label_match = re.match(
            r"^([ \t]*-[ \t]+)(name|run-name|description):(?:[ \t]*(.*?))?(\r?\n)?$",
            line, re.I,
        )
        if label_match:
            kept.append(line)
            index += 1
            continue
        key_match = re.match(r"""^(\s*)(?:([\'"])([A-Za-z_][A-Za-z0-9_-]*)\2|([A-Za-z_][A-Za-z0-9_-]*)):(?:\s*(.*?))?(\r?\n)?$""", line)
        if key_match:
            _indent, _key_quote, quoted_key, plain_key, value, _newline = key_match.groups()
            key = quoted_key or plain_key
            if key.lower() == "environment":
                # Never rename an approval-gated environment. A retired-provider
                # name requires manual reconciliation before this branch can migrate.
                if value and RETIRED_PROVIDER_RE.search(value):
                    raise MigrationError(
                        f"{path} references a retired provider in its deployment environment; preserving the approval gate and refusing migration"
                    )
                kept.append(line)
                index += 1
                continue
            if key.lower() in {"name", "run-name", "description"}:
                kept.append(line)
                index += 1
                continue
            if PROVIDER_SETTING_NAME_RE.fullmatch(key) or RETIRED_PROVIDER_FIELD_RE.search(key):
                # Removing a parent key and retaining its indented children can make
                # GitHub Actions YAML invalid. Only scalar provider settings are removed.
                key_indent = len(_indent)
                has_nested_children = False
                for child in lines[index + 1:]:
                    if not child.strip() or child.lstrip().startswith("#"):
                        continue
                    child_indent = len(child) - len(child.lstrip())
                    if child_indent <= key_indent:
                        break
                    has_nested_children = True
                    break
                if has_nested_children:
                    raise MigrationError(
                        f"{path} has provider-named YAML mapping {key!r} with children; refusing to invalidate workflow structure"
                    )
                index += 1
                continue
            if value and (RETIRED_PROVIDER_RE.search(value) or ATLAS_PROVIDER_SECRET_RE.search(value)):
                # Do not run generic token substitution on a YAML value: it can alter
                # an environment, URL, shell command, or approval/resource identifier.
                raise MigrationError(
                    f"{path} has provider-bearing YAML scalar {key!r}; refusing unsafe text substitution"
                )
            kept.append(line)
            index += 1
            continue

        # Unstructured provider-bearing lines are not safe to edit generically.
        if RETIRED_PROVIDER_RE.search(line) or ATLAS_PROVIDER_SECRET_RE.search(line):
            raise MigrationError(
                f"{path} has provider wiring outside a recognized YAML field; refusing unsafe text substitution: {stripped[:100]}"
            )
        kept.append(line)
        index += 1

    cleaned = "".join(_remove_empty_env(kept))
    if _workflow_has_active_provider_wiring(cleaned):
        raise MigrationError(f"{path} still contains active hosted-provider wiring after workflow cleanup")
    return cleaned


def scrub_ancillary_text(path: str, text: str) -> str:
    if path in {".github/workflows/production-deploy.yml", ".github/workflows/production-verification.yml"}:
        return scrub_workflow_ancillary(path, text)
    if path in {"deploy/cloud-run-deploy.sh", "deploy/cloud-run-worker-deploy.sh", "deploy/gcp-bootstrap.sh"}:
        return scrub_shell_deployment(path, text)
    if path == "metadata.json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MigrationError(f"metadata.json is invalid JSON: {exc}") from exc

        remove_sentinel = object()

        def scrub(value, key_context=""):
            remove = remove_sentinel
            if isinstance(value, dict):
                named_setting = next(
                    (item for key, item in value.items()
                     if str(key).lower() in {"name", "key", "setting", "variable"} and isinstance(item, str)),
                    None,
                )
                has_payload = any(
                    str(key).lower() in {"value", "secretref", "secret_ref", "version", "endpoint"}
                    for key in value
                )
                if isinstance(named_setting, str) and PROVIDER_SETTING_NAME_RE.fullmatch(named_setting) and has_payload:
                    return remove
                result = {}
                for key, item in value.items():
                    key_text = str(key)
                    if RETIRED_PROVIDER_FIELD_RE.search(key_text):
                        continue
                    if key_text.lower() in {"secretref", "secret_ref"} and isinstance(item, str) and PROVIDER_SETTING_NAME_RE.fullmatch(item):
                        continue
                    cleaned_item = scrub(item, str(key))
                    if cleaned_item is not remove:
                        result[key] = cleaned_item
                return result
            if isinstance(value, list):
                return [cleaned for item in value if (cleaned := scrub(item, key_context)) is not remove]
            if isinstance(value, str) and key_context.lower() in {"name", "key", "setting", "variable"} and PROVIDER_SETTING_NAME_RE.fullmatch(value):
                return ""
            if isinstance(value, str) and (RETIRED_PROVIDER_RE.search(value) or ATLAS_PROVIDER_SECRET_RE.search(value)):
                if re.fullmatch(r"(?i)(?:[a-z0-9]+_)*model(?:_(?:id|name|version))?", key_context):
                    return "atlas-native-strategy-v1"
                cleaned = SECRET_VERSION_RE.sub("", value)
                cleaned = KEY_ASSIGNMENT_RE.sub("", cleaned)
                cleaned = HOST_RE.sub("", cleaned)
                cleaned = ATLAS_PROVIDER_SECRET_RE.sub("", cleaned)
                cleaned = MODEL_RE.sub("atlas-native-strategy-v1", cleaned)
                # Descriptive/audit prose may mention historical providers; preserve it.
                return cleaned
            return value

        scrubbed = scrub(data)
        if scrubbed is remove_sentinel:
            scrubbed = {}
        return json.dumps(scrubbed, indent=2, ensure_ascii=False) + "\n"

    kept: list[str] = []
    is_markdown = path.lower().endswith(".md")
    markdown_fence_char: str | None = None
    markdown_fence_length = 0
    for line in text.splitlines(keepends=True):
        fence_match = re.match(r"^\s*(\x60{3,}|~{3,})(.*?)(?:\r?\n)?$", line)
        if is_markdown and fence_match:
            marker, suffix = fence_match.groups()
            if markdown_fence_char is None:
                markdown_fence_char = marker[0]
                markdown_fence_length = len(marker)
                kept.append(line)
                continue
            if (
                marker[0] == markdown_fence_char
                and len(marker) >= markdown_fence_length
                and not suffix.strip()
            ):
                markdown_fence_char = None
                markdown_fence_length = 0
                kept.append(line)
                continue
            # A shorter marker (e.g. three backticks inside a four-backtick
            # fence) is content rather than a closing delimiter.
        if not RETIRED_PROVIDER_RE.search(line):
            kept.append(line)
            continue
        if is_markdown and markdown_fence_char is not None:
            raise MigrationError(
                f"{path} contains provider-bearing code inside a Markdown code fence; "
                "refusing to rewrite an executable example"
            )
        candidate = SECRET_VERSION_RE.sub("", line)
        candidate = KEY_ASSIGNMENT_RE.sub("", candidate)
        candidate = HOST_RE.sub("", candidate)
        candidate = MODEL_RE.sub("atlas-native-strategy-v1", candidate)
        candidate = re.sub(r"(?i)\b(?:Gemini|Cerebras|AI Gateway)\b", "", candidate)
        if RETIRED_PROVIDER_RE.search(candidate):
            if (
                re.search(r"(?i)\b(?:gcloud|docker|curl|python|bash|run:)\b", candidate)
                and not is_markdown
            ):
                raise MigrationError(f"{path} has provider wiring inside an executable line; refusing automatic deletion")
            candidate = RETIRED_PROVIDER_RE.sub("legacy provider", candidate)
        if candidate.strip():
            kept.append(candidate)
    cleaned = "".join(_remove_empty_env(kept))
    if RETIRED_PROVIDER_RE.search(cleaned):
        raise MigrationError(f"{path} still contains retired-provider references")
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
        _git("restore", "--source=HEAD", "--staged", "--worktree", "--", path, check=False)
        if _git("cat-file", "-e", f"HEAD:{path}", check=False).returncode != 0:
            _git("rm", "-f", "--cached", "--ignore-unmatch", "--", path, check=False)
            target = ROOT / path
            if target.is_file() or target.is_symlink():
                target.unlink()
        return False
    return True


def apply_ancillary_patch(commit: str, path: str, extra_base: str, extra_commit: str) -> None:
    cleanup_patch_applied = _apply_path_patch(f"{commit}^", commit, path)
    if not cleanup_patch_applied and path in EXTRA_PATHS:
        # The Android/document extras need their separately reviewed patch. Preserve
        # their exact branch content when that patch conflicts; never use a broad text
        # scrub as a substitute for those path-specific reviews.
        return
    if path in EXTRA_PATHS and not _apply_path_patch(extra_base, extra_commit, path):
        # Preserve the exact conflicting Android/document path. If it still contains
        # active provider wiring, the final scan will reject this branch instead.
        return

    target = ROOT / path
    if target.exists():
        current = target.read_text(encoding="utf-8")
        # A failed patch against a divergent legacy deployment file is not proof that
        # the file is unsafe to migrate. The scrubber below is deliberately narrow:
        # it removes recognized provider wiring and raises MigrationError for mixed
        # commands/unknown constructs instead of silently discarding them. Always run
        # that semantic cleanup after patch conflicts on the ordinary ancillary paths.
        cleaned = scrub_ancillary_text(path, current)
        if cleaned != current:
            target.write_text(cleaned, encoding="utf-8")


def _metadata_has_active_provider_wiring(text: str) -> bool:
    """Permit inert descriptive prose, but reject real provider configuration and credentials."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return True

    def visit(value, key_context: str = "") -> bool:
        if isinstance(value, dict):
            named_setting = next(
                (item for key, item in value.items()
                 if str(key).lower() in {"name", "key", "setting", "variable"} and isinstance(item, str)),
                None,
            )
            has_payload = any(
                str(key).lower() in {"value", "secretref", "secret_ref", "version", "endpoint"}
                for key in value
            )
            if isinstance(named_setting, str) and PROVIDER_SETTING_NAME_RE.fullmatch(named_setting) and has_payload:
                return True
            for key, item in value.items():
                key_text = str(key)
                if RETIRED_PROVIDER_FIELD_RE.search(key_text):
                    return True
                if visit(item, key_text):
                    return True
            return False
        if isinstance(value, list):
            return any(visit(item, key_context) for item in value)
        if isinstance(value, str):
            if key_context.lower() in {"name", "key", "setting", "variable", "secretref", "secret_ref"} and PROVIDER_SETTING_NAME_RE.fullmatch(value):
                return True
            if (
                ATLAS_PROVIDER_SECRET_RE.search(value)
                or KEY_ASSIGNMENT_RE.search(value)
                or SECRET_VERSION_RE.search(value)
                or HOST_RE.search(value)
            ):
                return True
            if re.search(r"(?i)(?:provider|gateway|model|api[_ -]?key|secret|endpoint)", key_context) and RETIRED_PROVIDER_RE.search(value):
                return True
        return False

    return visit(data)


def validate_groq_research_module(source: str, path: str = "app/groq_ai.py") -> None:
    """Validate the optional Groq review API and allow only reviewed, resolvable imports."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise MigrationError(f"{path} is not valid Python; preserving the branch") from exc
    if RETIRED_PROVIDER_RE.search(source):
        raise MigrationError(f"{path} contains retired-provider wiring; preserving the branch")

    reviewers = [
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "groq_research_review"
    ]
    if len(reviewers) != 1:
        raise MigrationError(f"{path} must define exactly one async groq_research_review API")
    reviewer = reviewers[0]
    positional_parameters = reviewer.args.posonlyargs + reviewer.args.args
    positional = len(positional_parameters)
    required_positional = positional - len(reviewer.args.defaults)
    required_keyword_only = [
        argument.arg
        for argument, default in zip(reviewer.args.kwonlyargs, reviewer.args.kw_defaults)
        if default is None
    ]
    # The consumer supplies exactly two positional values. Optional extra positional
    # parameters and *args are safe; extra required positional or keyword-only values are not.
    if positional < 2 and reviewer.args.vararg is None:
        raise MigrationError(
            f"{path} groq_research_review must accept two positional arguments (research, intelligence)"
        )
    if required_positional > 2:
        raise MigrationError(
            f"{path} groq_research_review has more than two required positional parameters"
        )
    if required_keyword_only:
        raise MigrationError(
            f"{path} groq_research_review requires keyword-only parameters: {required_keyword_only}"
        )

    # This adapter runs at app startup. Do not preserve arbitrary imports that could
    # be missing on a branch or introduce side-effecting dependencies. These are the
    # canonical adapter's runtime modules and the exact local contracts it consumes.
    allowed_plain_imports = {
        "asyncio", "httpx", "json", "logging", "math", "re",
    }
    allowed_from_imports = {
        (0, "__future__"): {"annotations"},
        (0, "typing"): {"Any"},
        (1, "config"): {"settings"},
        (1, "prompt_safety"): {
            "UNTRUSTED_PREAMBLE", "suspicious_instruction_markers", "wrap_untrusted",
        },
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in allowed_plain_imports:
                    raise MigrationError(
                        f"{path} imports unreviewed or unavailable module {alias.name!r}; preserving the branch"
                    )
        elif isinstance(node, ast.ImportFrom):
            key = (node.level, node.module or "")
            allowed_names = allowed_from_imports.get(key)
            if allowed_names is None or any(
                alias.name == "*"
                or alias.name not in allowed_names
                or (key == (0, "__future__") and alias.asname is not None)
                for alias in node.names
            ):
                raise MigrationError(
                    f"{path} has unreviewed or unresolved import from {node.module!r}; preserving the branch"
                )



def migrate(canonical_commit: str, cleanup_commit: str, extra_base: str, extra_commit: str) -> None:
    # Canonical Native AI source and the original provider-removal patch baseline are
    # independent pins: changing one must not widen the other per-path diff.
    transform_ai_providers(canonical_commit)
    write_text("scripts/check_ai_models.py", canonical_text(canonical_commit, "scripts/check_ai_models.py"))
    for path in ("app/native_ai.py", "scripts/check_native_ai.py", "app/groq_ai.py", "app/prompt_safety.py"):
        target = ROOT / path
        if not target.exists():
            write_text(path, canonical_text(canonical_commit, path))
        elif path == "app/groq_ai.py":
            validate_groq_research_module(target.read_text(encoding="utf-8"), path)
            # Preserve compatible branch-specific Groq diagnostics while still rejecting
            # stale hosted-provider wiring; do not overwrite divergent unrelated logic.
        elif path == "app/prompt_safety.py":
            if target.read_text(encoding="utf-8") != canonical_text(canonical_commit, path):
                raise MigrationError(f"{path} differs from its reviewed canonical implementation; preserving the branch")
        elif PROVIDER_RE.search(target.read_text(encoding="utf-8")):
            raise MigrationError(f"{path} has a divergent provider-bearing implementation; refusing overwrite")

    transform_config()
    transform_strategy_ai(canonical_commit)
    transform_daily_research()
    transform_release_diagnostics(extra_commit)
    for path in ANCILLARY_PATHS:
        apply_ancillary_patch(cleanup_commit, path, extra_base, extra_commit)

    remaining = []
    for path in NATIVE_PATHS:
        target = ROOT / path
        if not target.is_file():
            continue
        contents = target.read_text(encoding="utf-8")
        if path == "metadata.json":
            if _metadata_has_active_provider_wiring(contents):
                remaining.append(path)
        elif path in {".github/workflows/production-deploy.yml", ".github/workflows/production-verification.yml"}:
            if _workflow_has_active_provider_wiring(contents):
                remaining.append(path)
        elif RETIRED_PROVIDER_RE.search(contents):
            remaining.append(path)
    if remaining:
        raise MigrationError("provider references remain in: " + ", ".join(remaining))

    for path in ("app/config.py", "app/daily_research.py", "app/strategy_ai.py", "app/ai_providers.py", "app/native_ai.py", "app/groq_ai.py", "app/main.py"):
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
    parser.add_argument("--canonical-commit", default=DEFAULT_CANONICAL_COMMIT)
    parser.add_argument("--cleanup-commit", default=DEFAULT_CLEANUP_COMMIT)
    parser.add_argument("--extra-base-commit", default=DEFAULT_EXTRA_BASE_COMMIT)
    parser.add_argument("--extra-commit", required=True)
    args = parser.parse_args()
    try:
        _git("cat-file", "-e", f"{args.canonical_commit}^{{commit}}")
        _git("cat-file", "-e", f"{args.cleanup_commit}^{{commit}}")
        _git("cat-file", "-e", f"{args.extra_base_commit}^{{commit}}")
        _git("cat-file", "-e", f"{args.extra_commit}^{{commit}}")
        migrate(args.canonical_commit, args.cleanup_commit, args.extra_base_commit, args.extra_commit)
    except (MigrationError, OSError, SyntaxError, json.JSONDecodeError) as exc:
        print(f"SAFE MIGRATION REFUSED: {exc}")
        return 1
    print("PASS: targeted Native AI migration preserved other branch files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
