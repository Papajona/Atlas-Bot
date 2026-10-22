from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import paper_staging


def _concrete_dispatch_routes(app):
    """Walk actual dispatch routes through FastAPI's lazy included-router wrappers."""
    pending = list(app.routes)
    seen = set()
    concrete = []
    while pending:
        route = pending.pop()
        if id(route) in seen:
            continue
        seen.add(id(route))
        path = getattr(route, "path", None)
        if isinstance(path, str):
            concrete.append(route)
            # A mounted route can itself own an ASGI application with child routes.
            children = getattr(getattr(route, "app", None), "routes", None)
            if children:
                pending.extend(children)
            continue
        children = getattr(route, "routes", None)
        if children is None:
            for attr in ("router", "original_router", "_router"):
                nested = getattr(route, attr, None)
                nested_routes = getattr(nested, "routes", None)
                if nested_routes is not None:
                    children = nested_routes
                    break
        if children is None:
            raise AssertionError(f"Cannot enumerate dispatchable route wrapper: {type(route).__name__}")
        pending.extend(list(children))
    return concrete


def test_paper_staging_asgi_exposes_only_health_and_paper_routes():
    routes = _concrete_dispatch_routes(paper_staging.app)
    assert len(routes) >= 3
    allowed = lambda path: (
        path in {"/healthz", "/readyz"}
        or path.startswith("/api/customer/paper/")
    )
    assert all(allowed(route.path) for route in routes)
    assert all(route.path != "/openapi.json" for route in routes)
    assert all(getattr(route, "methods", None) for route in routes), "non-HTTP or mounted route escaped enumeration"
    assert next(r for r in routes if r.path == "/healthz").methods == {"GET"}
    assert next(r for r in routes if r.path == "/readyz").methods == {"GET"}
    schema = paper_staging.app.openapi()["paths"]
    schema_pairs = {(path, method.upper()) for path, methods in schema.items() for method in methods}
    dispatch_pairs = {(route.path, method) for route in routes for method in route.methods}
    assert dispatch_pairs == schema_pairs, "a dispatchable method is missing from the public route schema"
    assert paper_staging.app.docs_url is None
    assert paper_staging.app.redoc_url is None
    assert paper_staging.app.openapi_url is None



def test_readyz_memory_limiter_enforces_quota_and_expires_old_requests(monkeypatch):
    limiter = paper_staging._readyz_request_times
    limiter.clear()
    monkeypatch.setattr(paper_staging, "_READYZ_RATE_LIMIT_PER_MINUTE", 2)
    monkeypatch.setattr(paper_staging, "_READYZ_RATE_MAX_CLIENTS", 10)

    assert paper_staging._allow_paper_readyz_request("client-a", now=100.0)
    assert paper_staging._allow_paper_readyz_request("client-a", now=101.0)
    assert not paper_staging._allow_paper_readyz_request("client-a", now=102.0)
    assert paper_staging._allow_paper_readyz_request("client-b", now=102.0)
    assert paper_staging._allow_paper_readyz_request("client-a", now=161.0)


def test_readyz_memory_limiter_bounds_client_state_and_fails_closed(monkeypatch):
    limiter = paper_staging._readyz_request_times
    limiter.clear()
    monkeypatch.setattr(paper_staging, "_READYZ_RATE_LIMIT_PER_MINUTE", 10)
    monkeypatch.setattr(paper_staging, "_READYZ_RATE_MAX_CLIENTS", 2)

    assert paper_staging._allow_paper_readyz_request("client-a", now=10.0)
    assert paper_staging._allow_paper_readyz_request("client-b", now=10.0)
    assert not paper_staging._allow_paper_readyz_request("client-c", now=11.0)
    assert len(limiter) == 2
    assert paper_staging._allow_paper_readyz_request("client-c", now=71.0)
    assert len(limiter) <= 2


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
        def one_or_none(self):
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
        def one_or_none(self):
            return SimpleNamespace(id=profile.id, auth_user_id=profile.auth_user_id, status=profile.status)

    class ReadOnlyDatabase:
        statement = None

        async def execute(self, query):
            self.statement = query
            return QueryResult()

    db = ReadOnlyDatabase()
    monkeypatch.setattr(paper_staging, "customer_claims", fake_customer_claims)

    result_profile, claims = asyncio.run(
        paper_staging.get_existing_customer("Bearer test", db)
    )

    assert result_profile.id == profile.id
    assert result_profile.auth_user_id == profile.auth_user_id
    assert result_profile.status == profile.status
    assert claims["aal"] == "aal2"
    selected = {column.name for column in db.statement.selected_columns}
    assert selected == {"id", "auth_user_id", "status"}
    assert "email" not in selected and "display_name" not in selected
