"""Read Alembic revision graph metadata without importing application dependencies.

Production deployment uses this to compare the live database revision against the
exact migration graph embedded in the checked-out release before rollout.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


def _literal_module_assignment(tree: ast.Module, name: str, path: Path) -> Any:
    values: list[Any] = []
    for node in tree.body:
        value_node = None
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            value_node = node.value
        if value_node is not None:
            try:
                values.append(ast.literal_eval(value_node))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{path} has a non-literal {name} assignment") from exc
    if len(values) != 1:
        raise ValueError(f"{path} must define exactly one literal {name} assignment")
    return values[0]


def expected_alembic_heads(versions_dir: Path) -> set[str]:
    """Return all Alembic graph heads, rejecting duplicate, missing, or cyclic parents."""
    revisions: dict[str, tuple[str, ...]] = {}
    for path in sorted(Path(versions_dir).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        revision = _literal_module_assignment(tree, "revision", path)
        down_revision = _literal_module_assignment(tree, "down_revision", path)
        if not isinstance(revision, str) or not revision.strip():
            raise ValueError(f"{path} has an invalid revision identifier")
        if revision in revisions:
            raise ValueError(f"duplicate Alembic revision identifier: {revision}")
        if down_revision is None:
            parents: tuple[str, ...] = ()
        elif isinstance(down_revision, str):
            parents = (down_revision,)
        elif isinstance(down_revision, (tuple, list)) and all(
            isinstance(parent, str) and parent.strip() for parent in down_revision
        ):
            parents = tuple(down_revision)
        else:
            raise ValueError(f"{path} has an invalid down_revision declaration")
        revisions[revision] = parents

    if not revisions:
        raise ValueError(f"no Alembic revision files found in {versions_dir}")

    referenced = {parent for parents in revisions.values() for parent in parents}
    missing = referenced - set(revisions)
    if missing:
        raise ValueError(f"Alembic revision graph has missing parents: {sorted(missing)}")

    remaining = set(revisions)
    resolved: set[str] = set()
    while remaining:
        ready = {
            revision for revision in remaining
            if all(parent in resolved for parent in revisions[revision])
        }
        if not ready:
            raise ValueError(f"Alembic revision graph contains a cycle: {sorted(remaining)}")
        resolved.update(ready)
        remaining.difference_update(ready)

    heads = set(revisions) - referenced
    if not heads:
        raise ValueError("Alembic revision graph has no head")
    return heads
