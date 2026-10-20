from __future__ import annotations

import ast
from pathlib import Path


def test_tron_scanner_uses_captured_headers_for_every_client_request():
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    scanner = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_scan_one"
        and any(isinstance(default, ast.Name) and default.id == "headers" for default in node.args.defaults)
    )
    requests = [
        node for node in ast.walk(scanner)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "client"
        and node.func.attr in {"get", "post"}
    ]
    assert len(requests) == 3
    for request in requests:
        values = [kw.value for kw in request.keywords if kw.arg == "headers"]
        assert len(values) == 1, f"Missing explicit headers at line {request.lineno}"
        assert isinstance(values[0], ast.Name) and values[0].id == "_headers", (
            f"Request at line {request.lineno} must use the captured _headers value"
        )
