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


def _named_references(tree: ast.AST, name: str) -> list[ast.Name]:
    # asyncio.to_thread(helper, ...) passes the helper as a callback, so it is a
    # Name reference rather than a direct Call node. Count actual loaded references.
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id == name
    ]


def test_customer_market_context_helper_is_referenced_by_runtime_code() -> None:
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert len(_named_references(tree, "_server_paper_market_context")) >= 3


def test_platform_paper_step_uses_bar_simulator_with_server_context_when_calibrated() -> None:
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    route = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "paper_step"
    )
    assert _named_calls(route, "advance_platform_paper_orders")
    assert _named_references(route, "_server_paper_market_context")
    assert _named_references(route, "configured_paper_execution_settings")


def test_active_platform_and_autonomous_customer_runtime_call_the_calibrated_model() -> None:
    for path in (ROOT / "app" / "execution.py", ROOT / "app" / "customer_paper_flow.py"):
        source = path.read_text(encoding="utf-8")
        assert "from .paper_execution_model import" in source, path.name
        assert "estimate_paper_fill(" in source, path.name
        assert "configured_paper_execution_settings(settings)" in source, path.name
    manual_api = (ROOT / "app" / "customer_paper_api.py").read_text(encoding="utf-8")
    assert "market_impact_bps(" in manual_api
    assert "estimate_paper_fill(" not in manual_api


def test_readiness_doc_distinguishes_market_context_from_calibrated_fill_model() -> None:
    doc = (ROOT / "docs" / "PAPER_TESTING_READINESS_2026-10-15.md").read_text(encoding="utf-8")
    assert "The calibrated estimator is now wired into" in doc
    assert "manual customer API advancement remains heuristic" in doc
    assert "Partial calibration configuration fails closed" in doc
