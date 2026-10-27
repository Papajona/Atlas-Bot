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
    if isinstance(node, ast.Delete):
        return {name for target in node.targets for name in _assigned_names(target)}
    # Module-level control-flow targets also bind names and can shadow imports
    # installed by the canonical facade. Fail closed on any such collision.
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return _assigned_names(node.target)
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return {
            name
            for item in node.items
            if item.optional_vars is not None
            for name in _assigned_names(item.optional_vars)
        }
    if isinstance(node, ast.AugAssign):
        return _assigned_names(node.target)
    if isinstance(node, ast.Expr):
        # Assignment expressions in module-level expressions bind in module scope,
        # but a walrus inside a lambda body binds only in that lambda's local scope.
        names: set[str] = set()
        pending = [node.value]
        while pending:
            part = pending.pop()
            if isinstance(part, ast.Lambda):
                # Lambda defaults execute in the enclosing scope; the body does not.
                pending.extend(part.args.defaults)
                pending.extend(value for value in part.args.kw_defaults if value is not None)
                continue
            if isinstance(part, ast.NamedExpr):
                names.update(_assigned_names(part.target))
            pending.extend(ast.iter_child_nodes(part))
        return names
    if isinstance(node, ast.NamedExpr):
        return _assigned_names(node.target)
    if isinstance(node, ast.Import):
        return {alias.asname or alias.name.split(".", 1)[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom):
        return {alias.asname or alias.name for alias in node.names if alias.name != "*"}
    return set()


def _assigned_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _assigned_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return {name for item in target.elts for name in _assigned_names(item)}
    return set()


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
    extra_defined_names: list[set[str]] = []
    extra_definition_lines: list[int] = []
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
        defined_names = _defined_names(node)
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
        extras.append(segment.rstrip())
        extra_defined_names.append(defined_names)
        extra_definition_lines.append(node.lineno)

    used_names: set[str] = set()
    for segment in extras:
        tree = ast.parse(segment)
        used_names.update(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))

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
        for module_node in module_tree.body:
            for defined_name in _defined_names(module_node):
                last_definitions[defined_name] = module_node
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
        and (
            (isinstance(provider_body[0].value.value.func, ast.Name)
             and provider_body[0].value.value.func.id in provider_delegations)
            or (isinstance(provider_body[0].value.value.func, ast.Attribute)
                and provider_body[0].value.value.func.attr in provider_delegations)
        )
    )
    if direct_provider_trampoline:
        provider_call = provider_body[0].value.value
        direct_provider_trampoline = (
            len(provider_call.args) == 1
            and isinstance(provider_call.args[0], ast.Name)
            and provider_call.args[0].id == "prompt"
            and not provider_call.keywords
        )
    if not direct_provider_trampoline:
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
    # Check retained schema/helper contracts before transplanting the canonical generator.
    canonical_spec = next(
        (node for node in new_tree.body if isinstance(node, ast.ClassDef) and node.name == "AIStrategySpec"),
        None,
    )
    canonical_context = next(
        (node for node in new_tree.body
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_apply_market_context"),
        None,
    )
    if canonical_spec is not None:
        legacy_spec = next(
            (node for node in old_tree.body if isinstance(node, ast.ClassDef) and node.name == "AIStrategySpec"),
            None,
        )
        if legacy_spec is None:
            raise MigrationError("legacy strategy module lacks AIStrategySpec required by the canonical generator")
        is_model, effective_fields = _class_contract_info(path, old_tree, legacy_spec)
        required_spec_fields = {"asset", "symbols", "timeframes", "strategy_family", "validation_warnings"}
        if not is_model or not required_spec_fields.issubset(effective_fields):
            missing_fields = sorted(required_spec_fields - effective_fields)
            detail = ", ".join(missing_fields) if missing_fields else "a verifiable Pydantic BaseModel ancestry"
            raise MigrationError(
                "legacy AIStrategySpec does not satisfy the canonical generator contract; "
                f"missing or unverifiable contract: {detail}"
            )

        if canonical_context is not None:
            legacy_context = next(
                (node for node in old_tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "_apply_market_context"),
                None,
            )
            if legacy_context is None:
                raise MigrationError(
                    "legacy strategy module lacks a synchronous _apply_market_context required by the canonical generator"
                )
            arguments = legacy_context.args
            positional = [*arguments.posonlyargs, *arguments.args]
            parameter_names = {arg.arg for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)}
            required_context_keywords = {"asset", "symbol", "timeframe"}
            supports_keywords = arguments.kwarg is not None or required_context_keywords.issubset(parameter_names)
            if not positional or positional[0].arg != "spec" or not supports_keywords:
                raise MigrationError(
                    "legacy _apply_market_context signature is incompatible with canonical keyword calls"
                )

        config_path = ROOT / "app/config.py"
        if not config_path.is_file():
            raise MigrationError("app/config.py is absent; cannot verify canonical strategy risk configuration")
        config_tree = ast.parse(config_path.read_text(encoding="utf-8"), filename=str(config_path))
        settings_class = next(
            (node for node in config_tree.body if isinstance(node, ast.ClassDef) and node.name == "Settings"),
            None,
        )
        if settings_class is None:
            raise MigrationError("app/config.py lacks a statically verifiable Settings class")
        _, settings_fields = _class_contract_info(config_path, config_tree, settings_class)
        if "risk_per_trade" not in settings_fields:
            raise MigrationError(
                "legacy Settings class does not define or inherit risk_per_trade required by canonical strategy generation"
            )

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

    # Scope-aware references distinguish a retained helper's parameter from a real
    # reference to a removed module global.
    retained_module = ast.Module(body=retained_nodes, type_ignores=[])
    try:
        retained_symbols = symtable.symtable(ast.unparse(retained_module), str(path), "exec")
    except (SyntaxError, ValueError) as exc:
        raise MigrationError("unable to resolve retained strategy helper scopes safely") from exc
    removed_local_dependencies = {"SYSTEM_PROMPT", "_strategy_prompt", "_generate_with_provider"}
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

    # __all__ is string-based, so Name-node dependency scans do not see removed exports.
    for retained_node in retained_nodes:
        targets = (
            retained_node.targets if isinstance(retained_node, ast.Assign)
            else [retained_node.target] if isinstance(retained_node, ast.AnnAssign)
            else []
        )
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
            continue
        try:
            exports = ast.literal_eval(retained_node.value) if retained_node.value is not None else None
        except (ValueError, TypeError):
            exports = None
        if not isinstance(exports, (list, tuple, set)) or not all(isinstance(export, str) for export in exports):
            raise MigrationError("strategy module has dynamic __all__; refusing to remove names that may be exported")
        lost_exports = sorted(removed_local_dependencies & set(exports))
        if lost_exports:
            raise MigrationError(
                "strategy module __all__ exports definitions removed by migration: " + ", ".join(lost_exports)
            )

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

    # Prove the canonical generator's global loads resolve after the migration.
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
        if isinstance(retained_node, ast.AnnAssign) and retained_node.value is None:
            continue
        available_names.update(_defined_names(retained_node))
    remappable_names = {"NativeAIError", "compose_strategy_spec"}
    missing_dependencies = sorted(canonical_global_loads - available_names - remappable_names)
    if missing_dependencies:
        raise MigrationError(
            "canonical strategy generator depends on missing module binding(s): "
            + ", ".join(missing_dependencies)
            + "; refusing to publish a broken strategy module"
        )

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
        # Preserve original blank lines, but do not keep lines emptied by provider removal.
        if line.strip() or not original.strip():
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
                    key: scrub(item, str(key))
                    for key, item in value.items()
                    if not PROVIDER_FIELD_RE.search(str(key))
                }
            if isinstance(value, list):
                return [scrub(item, key_context) for item in value]
            if isinstance(value, str) and PROVIDER_RE.search(value):
                if re.fullmatch(r"(?i)(?:[a-z0-9]+_)*model(?:_(?:id|name|version))?", key_context):
                    return "atlas-native-strategy-v1"
                # Remove actual credentials/endpoints, but preserve descriptive and audit
                # values that merely mention a provider name.
                cleaned = SECRET_VERSION_RE.sub("", value)
                cleaned = KEY_ASSIGNMENT_RE.sub("", cleaned)
                cleaned = HOST_RE.sub("", cleaned)
                cleaned = MODEL_RE.sub("atlas-native-strategy-v1", cleaned)
                return cleaned
            return value

        scrubbed = scrub(data)
        # Keep whitespace, key order, and formatting stable for a semantic no-op.
        if scrubbed == data:
            return text
        encoded = json.dumps(scrubbed, indent=2, ensure_ascii=False) + "\n"
        json.loads(encoded)
        return encoded

    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        if path == ".env.example" and line.lstrip().startswith("#") and (
            re.search(r"(?i)optional server-side.*(?:provider gateway|ai gateway|openai-compatible gateway)", line)
            or re.search(r"(?i)keep the key in server Secret Manager", line)
        ):
            continue
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


def migrate(canonical_commit: str, cleanup_commit: str, extra_base: str, extra_commit: str) -> None:
    # Canonical Native AI source and the original provider-removal patch baseline are
    # independent pins: changing one must not widen the other per-path diff.
    transform_ai_providers(canonical_commit)
    write_text("scripts/check_ai_models.py", canonical_text(canonical_commit, "scripts/check_ai_models.py"))
    for path in ("app/native_ai.py", "scripts/check_native_ai.py"):
        target = ROOT / path
        if not target.exists():
            write_text(path, canonical_text(canonical_commit, path))
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
