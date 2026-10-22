from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import paper_staging


def test_paper_staging_asgi_exposes_only_health_and_paper_routes():
    # Recent FastAPI releases retain included routers as internal router objects
    # instead of flattening every route into app.routes. OpenAPI's generated path
    # map is the stable public view of the mounted endpoints.
    paths = set(paper_staging.app.openapi()["paths"])

    assert "/healthz" in paths
    assert "/readyz" in paths
    assert any(path.startswith("/api/customer/paper/") for path in paths)
    assert all(
        path in {"/healthz", "/readyz"} or path.startswith("/api/customer/paper/")
        for path in paths
    )
    assert paper_staging.app.docs_url is None
    assert paper_staging.app.redoc_url is None
    assert paper_staging.app.openapi_url is None


def test_paper_customer_auth_rejects_aal1_before_querying_database(monkeypatch):
    async def fake_customer_claims(_authorization, require_aal2=True):
        return {"sub": "supabase-user-1", "aal": "aal1"}

    class ReadOnlyDatabase:
        execute_calls = 0

        async def execute(self, _query):
            self.execute_calls += 1
            raise AssertionError("database must not be queried for an AAL1 token")

    db = ReadOnlyDatabase()
    monkeypatch.setattr(paper_staging, "customer_claims", fake_customer_claims)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(paper_staging.get_existing_customer("Bearer test", db))

    assert exc.value.status_code == 403
    assert db.execute_calls == 0


def test_paper_customer_auth_requires_an_existing_profile_and_never_self_registers(monkeypatch):
    async def fake_customer_claims(_authorization, require_aal2=True):
        return {"sub": "supabase-user-2", "aal": "aal2"}

    class QueryResult:
        def scalar_one_or_none(self):
            return None

    class ReadOnlyDatabase:
        execute_calls = 0

        async def execute(self, _query):
            self.execute_calls += 1
            return QueryResult()

    db = ReadOnlyDatabase()
    monkeypatch.setattr(paper_staging, "customer_claims", fake_customer_claims)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(paper_staging.get_existing_customer("Bearer test", db))

    assert exc.value.status_code == 403
    assert "already exist" in exc.value.detail
    assert db.execute_calls == 1
    assert not hasattr(db, "add")
    assert not hasattr(db, "flush")


def test_paper_customer_auth_returns_only_an_active_existing_profile(monkeypatch):
    profile = SimpleNamespace(id=42, auth_user_id="supabase-user-3", status="ACTIVE")

    async def fake_customer_claims(_authorization, require_aal2=True):
        return {"sub": "supabase-user-3", "aal": "aal2"}

    class QueryResult:
        def scalar_one_or_none(self):
            return profile

    class ReadOnlyDatabase:
        async def execute(self, _query):
            return QueryResult()

    monkeypatch.setattr(paper_staging, "customer_claims", fake_customer_claims)

    result_profile, claims = asyncio.run(
        paper_staging.get_existing_customer("Bearer test", ReadOnlyDatabase())
    )

    assert result_profile is profile
    assert claims["aal"] == "aal2"
