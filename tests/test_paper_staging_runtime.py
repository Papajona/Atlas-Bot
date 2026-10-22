from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.schemas import MarketRequest

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



def test_staging_paper_request_accepts_short_history_without_relaxing_public_model():
    with pytest.raises(ValidationError):
        MarketRequest(days=1)
    assert paper_staging.PaperMarketRequest(days=1).days == 1
    assert paper_staging.PaperMarketRequest(days=30).days == 30
    with pytest.raises(ValidationError):
        paper_staging.PaperMarketRequest(days=31)


def test_readiness_bearer_token_fails_closed():
    assert not paper_staging._readiness_token_matches(None, "correct-secret")
    assert not paper_staging._readiness_token_matches("Bearer wrong-secret", "correct-secret")
    assert not paper_staging._readiness_token_matches("Basic correct-secret", "correct-secret")
    assert not paper_staging._readiness_token_matches("Bearer correct-secret", "")
    assert paper_staging._readiness_token_matches("Bearer correct-secret", "correct-secret")
    assert paper_staging._readiness_token_matches("bearer  correct-secret ", "correct-secret")


def test_readyz_rejects_unauthenticated_probe_before_database(monkeypatch):
    monkeypatch.setenv("ATLAS_STAGING_READINESS_TOKEN", "correct-secret")

    async def forbidden_preflight():
        raise AssertionError("unauthenticated readiness must be rejected before DB preflight")

    monkeypatch.setattr(paper_staging, "_cached_paper_runtime_preflight", forbidden_preflight)
    client = TestClient(paper_staging.app)

    missing = client.get("/readyz")
    invalid = client.get("/readyz", headers={"Authorization": "Bearer wrong-secret"})

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert missing.json()["reason"] == "READINESS_AUTHENTICATION_REQUIRED"
    assert invalid.json()["reason"] == "READINESS_AUTHENTICATION_REQUIRED"


def test_customer_api_limiter_caps_per_ip_and_per_auth_identity(monkeypatch):
    limiter = paper_staging._paper_api_request_times
    limiter.clear()
    monkeypatch.setattr(paper_staging, "_PAPER_ORDER_RATE_LIMIT_PER_MINUTE", 2)
    monkeypatch.setattr(paper_staging, "_PAPER_API_RATE_MAX_BUCKETS", 4)

    assert paper_staging._allow_paper_api_request("paper:ip:client-a", limit=2, now=5.0)
    assert paper_staging._allow_paper_api_request("paper:auth:token-a", limit=2, now=5.0)
    assert paper_staging._allow_paper_api_request("paper:ip:client-a", limit=2, now=6.0)
    assert paper_staging._allow_paper_api_request("paper:auth:token-a", limit=2, now=6.0)
    assert not paper_staging._allow_paper_api_request("paper:ip:client-a", limit=2, now=7.0)
    assert not paper_staging._allow_paper_api_request("paper:auth:token-a", limit=2, now=7.0)
    assert paper_staging._allow_paper_api_request("paper:ip:client-a", limit=2, now=66.0)


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
