"""Regression coverage for the reviewed administrator RBAC boundary.

These tests deliberately verify the exact endpoint-to-role mappings introduced by
the hardening patch and the actual require_role semantics. They do not infer
permissions from role rank: Atlas requires an explicit matching role.
"""
import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")


ENDPOINT_ROLES = {
    "admin_list_tron_sweeps": "READ_ONLY",
    "enable_forex_demo": "OPERATIONS",
    "execute_forex_demo": "OPERATIONS",
    "train": "OPERATIONS",
    "research_run_daily": "OPERATIONS",
    "model_experiments_history": "READ_ONLY",
    "paper_step": "OPERATIONS",
    "execute": "OPERATIONS",
    "reconcile_api": "OPERATIONS",
}


def _function_block(name: str) -> str:
    marker = f"async def {name}("
    start = MAIN.index(marker)
    next_defs = [
        MAIN.find("\nasync def ", start + len(marker)),
        MAIN.find("\n@app.", start + len(marker)),
    ]
    ends = [i for i in next_defs if i != -1]
    return MAIN[start:min(ends) if ends else len(MAIN)]


@pytest.mark.parametrize("name,required_role", ENDPOINT_ROLES.items())
def test_reviewed_endpoint_has_exact_rbac_gate(name, required_role):
    block = _function_block(name)
    auth_call = "await auth(x_admin_token, authorization)"
    assert f"claims = {auth_call}" in block
    assert block.count(auth_call) == 1
    assert f'await require_role(claims, "{required_role}")' in block


def _run(coro):
    return asyncio.run(coro)


def test_require_role_allows_exact_role(monkeypatch):
    from app import admin_rbac

    async def fake_roles(_uid):
        return {"OPERATIONS"}

    monkeypatch.setattr(admin_rbac, "get_roles", fake_roles)
    claims = {"sub": "operator-1", "auth_method": "supabase"}

    assert _run(admin_rbac.require_role(claims, "OPERATIONS")) == "OPERATIONS"


def test_require_role_denies_wrong_role(monkeypatch):
    from app import admin_rbac

    async def fake_roles(_uid):
        return {"READ_ONLY"}

    monkeypatch.setattr(admin_rbac, "get_roles", fake_roles)
    claims = {"sub": "reader-1", "auth_method": "supabase"}

    with pytest.raises(HTTPException) as exc:
        _run(admin_rbac.require_role(claims, "OPERATIONS"))
    assert exc.value.status_code == 403


def test_require_role_does_not_use_rank_to_escalate(monkeypatch):
    from app import admin_rbac

    async def fake_roles(_uid):
        return {"OPERATIONS"}

    monkeypatch.setattr(admin_rbac, "get_roles", fake_roles)
    claims = {"sub": "operator-1", "auth_method": "supabase"}

    with pytest.raises(HTTPException) as exc:
        _run(admin_rbac.require_role(claims, "RISK_OFFICER"))
    assert exc.value.status_code == 403


def test_require_role_allows_administrator(monkeypatch):
    from app import admin_rbac

    async def fake_roles(_uid):
        return {"ADMINISTRATOR"}

    monkeypatch.setattr(admin_rbac, "get_roles", fake_roles)
    claims = {"sub": "admin-1", "auth_method": "supabase"}

    assert _run(admin_rbac.require_role(claims, "OPERATIONS")) == "ADMINISTRATOR"


def test_require_role_allows_legacy_admin_token():
    from app import admin_rbac

    claims = {"auth_method": "legacy_admin_token"}

    assert _run(admin_rbac.require_role(claims, "OPERATIONS")) == "ADMINISTRATOR"


def test_require_role_denies_unassigned_user(monkeypatch):
    from app import admin_rbac

    async def fake_roles(_uid):
        return set()

    monkeypatch.setattr(admin_rbac, "get_roles", fake_roles)
    claims = {"sub": "unassigned-1", "auth_method": "supabase"}

    with pytest.raises(HTTPException) as exc:
        _run(admin_rbac.require_role(claims, "READ_ONLY"))
    assert exc.value.status_code == 403
