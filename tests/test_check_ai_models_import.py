"""Regression coverage for the direct-script import bootstrap."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ai_model_check_adds_repository_root_before_importing_app_config():
    source = (ROOT / "scripts" / "check_ai_models.py").read_text()
    tree = ast.parse(source)
    statements = tree.body
    root_setup = next(
        i for i, node in enumerate(statements)
        if isinstance(node, ast.If)
        and any(isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                and child.func.attr == "insert" for child in ast.walk(node))
    )
    config_import = next(
        i for i, node in enumerate(statements)
        if isinstance(node, ast.ImportFrom)
        and node.module == "app.config"
    )
    assert root_setup < config_import
