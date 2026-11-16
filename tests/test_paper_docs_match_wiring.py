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
    assert "Calibrated fill-model integration is now implemented" in doc
    assert "Manual customer API advancement remains a separate" in doc
    assert "Partial calibration configuration fails closed" in doc


def test_calibrated_platform_quote_preflight_precedes_order_cursor_advancement() -> None:
    source = (ROOT / "app" / "execution.py").read_text(encoding="utf-8")
    context_validation = source.index("calibrated_context_valid = context_valid")
    order_guard = source.index("if not calibrated_context_valid:", context_validation)
    advance = source.index(
        "order_state.bars_elapsed = int(order_state.bars_elapsed or 0) + 1",
        order_guard,
    )
    assert context_validation < order_guard < advance
    assert "and context_bar_at == bar_at" in source
    assert "The verified closed OHLC bar is still scanned below for exits." in source


def test_platform_protective_exit_is_not_rejected_or_ttl_cancelled_by_model() -> None:
    source = (ROOT / "app" / "execution.py").read_text(encoding="utf-8")
    assert "allow_simulated_rejection=not is_triggered_exit" in source
    assert "and not is_triggered_exit" in source
    assert 'execution_model_record["deferred_quantity"]' in source


def test_customer_protective_exit_remainder_is_deferred_not_expired() -> None:
    source = (ROOT / "app" / "customer_paper_flow.py").read_text(encoding="utf-8")
    assert "allow_simulated_rejection=not is_protective_exit" in source
    assert "and not is_protective_exit" in source
    assert 'execution_model_record["deferred_quantity"]' in source
    assert "Paper execution calibration configuration is incomplete" in source


def test_customer_ledger_does_not_persist_clipped_calibrated_fill_metadata() -> None:
    source = (ROOT / "app" / "customer_paper_lifecycle.py").read_text(encoding="utf-8")
    assert "shared bar capacity changed after calibrated fill estimation" in source
    assert "correlation-group capacity changed after calibrated fill estimation" in source
    assert "SELECT id, payload_sha256, metadata_json FROM customer_paper.fills" in source
