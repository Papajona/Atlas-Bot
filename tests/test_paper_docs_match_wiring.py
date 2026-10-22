"""Keep paper readiness claims aligned with distinct runtime market-context/fill-model paths."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _named_calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def test_customer_market_context_helper_is_called_by_runtime_code() -> None:
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    # FunctionDef is not a Call, so these count actual runtime call sites.
    assert len(_named_calls(tree, "_server_paper_market_context")) >= 2


def test_platform_paper_step_uses_bar_driven_simulator() -> None:
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    route = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "paper_step"
    )
    assert _named_calls(route, "advance_platform_paper_orders")


def test_active_runtime_does_not_call_standalone_calibrated_model() -> None:
    runtime_paths = [
        ROOT / "app" / "main.py",
        ROOT / "app" / "execution.py",
        ROOT / "app" / "customer_paper_flow.py",
        ROOT / "app" / "worker.py",
    ]
    for path in runtime_paths:
        source = path.read_text(encoding="utf-8")
        assert "from .paper_execution_model import" not in source, path.name
        assert "from app.paper_execution_model import" not in source, path.name
        assert "estimate_paper_fill(" not in source, path.name
        assert "calibrated_model_available(" not in source, path.name


def test_readiness_doc_distinguishes_market_context_from_calibrated_fill_model() -> None:
    doc = (ROOT / "docs" / "PAPER_TESTING_READINESS_2026-10-15.md").read_text(encoding="utf-8")
    assert "Calibrated fill-model integration remains incomplete" in doc
    assert "Customer-paper controller/submission paths call" in doc
    assert "api/paper/step does not call that helper" in doc
    assert "platform paper orders because calibrated-model evidence is absent" in doc
