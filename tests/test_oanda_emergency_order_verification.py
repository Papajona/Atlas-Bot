"""Malformed OANDA order rows may not be counted as canceled or closed."""
from pathlib import Path
import ast


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "app/execution.py").read_text()


def _function(name):
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(SOURCE, node) or ""
    raise AssertionError(f"{name} not found")


def test_oanda_emergency_cancel_fails_closed_for_missing_order_state():
    emergency = _function("emergency_stop")
    assert "OANDA order row lacks a state; refusing to treat it as non-open" in emergency
    assert "OANDA returned a malformed order row; cannot confirm the order book is clear" in emergency
    assert "still_open.append(str(order.get(\"id\") or \"<missing-order-id>\"))" in emergency
