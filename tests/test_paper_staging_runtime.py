from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

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




@pytest.mark.postgres
@pytest.mark.skipif(
    not os.getenv("ATLAS_POSTGRES_URL"),
    reason="requires the disposable PostgreSQL service provisioned by CI",
)
def test_primary_reader_preflight_membership_allowlist_is_behaviorally_exact():
    predicate = paper_staging._SAFE_SUPABASE_MANAGED_MEMBERSHIP_PREDICATE

    async def run():
        engine = create_async_engine(os.environ["ATLAS_POSTGRES_URL"])
        try:
            async with engine.connect() as conn:
                cases = [
                    (
                        {
                            "member_name": "postgres",
                            "grantor_name": "supabase_admin",
                            "admin_option": True,
                            "inherit_option": False,
                            "set_option": False,
                        },
                        0,
                    ),
                    (
                        {
                            "member_name": "postgres",
                            "grantor_name": "unexpected_grantor",
                            "admin_option": True,
                            "inherit_option": False,
                            "set_option": False,
                        },
                        1,
                    ),
                    (
                        {
                            "member_name": "unexpected_member",
                            "grantor_name": "supabase_admin",
                            "admin_option": True,
                            "inherit_option": False,
                            "set_option": False,
                        },
                        1,
                    ),
                    (
                        {
                            "member_name": "postgres",
                            "grantor_name": "supabase_admin",
                            "admin_option": False,
                            "inherit_option": False,
                            "set_option": False,
                        },
                        1,
                    ),
                    (
                        {
                            "member_name": "postgres",
                            "grantor_name": "supabase_admin",
                            "admin_option": True,
                            "inherit_option": True,
                            "set_option": False,
                        },
                        1,
                    ),
                    (
                        {
                            "member_name": "postgres",
                            "grantor_name": "supabase_admin",
                            "admin_option": True,
                            "inherit_option": False,
                            "set_option": True,
                        },
                        1,
                    ),
                ]
                query = text(
                    f"""
                    WITH membership_input AS (
                        SELECT CAST(:member_name AS text) AS member_name,
                               CAST(:grantor_name AS text) AS grantor_name,
                               CAST(:admin_option AS boolean) AS admin_option,
                               CAST(:inherit_option AS boolean) AS inherit_option,
                               CAST(:set_option AS boolean) AS set_option
                    )
                    SELECT count(*) FILTER (WHERE NOT ({predicate}))::integer AS member_count
                    FROM membership_input AS m
                    CROSS JOIN LATERAL (SELECT m.member_name AS rolname) AS member_role
                    CROSS JOIN LATERAL (SELECT m.grantor_name AS rolname) AS grantor_role
                    """
                )
                for membership, expected_member_count in cases:
                    row = (
                        await conn.execute(query, membership)
                    ).mappings().one()
                    member_count = int(row["member_count"])
                    assert member_count == expected_member_count, membership
                    assert paper_staging._primary_reader_membership_gate_passes(
                        member_count
                    ) is (expected_member_count == 0)
        finally:
            await engine.dispose()

    asyncio.run(run())

def test_staging_paper_request_accepts_short_history_without_relaxing_public_model():
    with pytest.raises(ValidationError):
        MarketRequest(days=1)
    assert paper_staging.PaperMarketRequest(days=1).days == 1
    assert paper_staging.PaperMarketRequest(days=30).days == 30
    with pytest.raises(ValidationError):
        paper_staging.PaperMarketRequest(days=31)


def test_readiness_bearer_token_is_purpose_bound_and_fails_closed():
    database_url = "postgresql+asyncpg://atlas_customer_paper:private-test-password@db.example/postgres"
    token = paper_staging._readiness_bearer_token(database_url)
    assert len(token) == 64
    assert not paper_staging._readiness_token_matches(None, database_url)
    assert not paper_staging._readiness_token_matches("Bearer wrong-secret", database_url)
    assert not paper_staging._readiness_token_matches("Basic " + token, database_url)
    assert not paper_staging._readiness_token_matches("Bearer " + token, "")
    assert paper_staging._readiness_token_matches("Bearer " + token, database_url)
    assert paper_staging._readiness_token_matches("bearer  " + token + " ", database_url)
    assert not paper_staging._readiness_token_matches(
        "Bearer " + token, database_url + "?different-secret-version"
    )


def test_readyz_rejects_unauthenticated_probe_before_database(monkeypatch):
    monkeypatch.setattr(
        paper_staging.settings,
        "customer_paper_database_url",
        "postgresql+asyncpg://atlas_customer_paper:private-test-password@db.example/postgres",
    )

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


def test_customer_middleware_separates_read_and_order_quotas_and_normalizes_tokens(monkeypatch):
    paper_staging._paper_api_request_times.clear()
    monkeypatch.setattr(paper_staging, "_PAPER_API_RATE_LIMIT_PER_MINUTE", 2)
    monkeypatch.setattr(paper_staging, "_PAPER_ORDER_RATE_LIMIT_PER_MINUTE", 1)
    monkeypatch.setattr(paper_staging, "_PAPER_API_IP_RATE_LIMIT_PER_MINUTE", 100)
    monkeypatch.setattr(paper_staging, "_trusted_client_ip", lambda _request: "shared-cloud-run-peer")

    test_app = FastAPI()
    test_app.middleware("http")(paper_staging._paper_customer_api_rate_limit)

    @test_app.get("/api/customer/paper/balances")
    async def balances():
        return {"ok": True}

    @test_app.post("/api/customer/paper/orders")
    async def create_order():
        return {"ok": True}

    client = TestClient(test_app)
    headers = {"Authorization": "Bearer stable-token"}

    assert client.get("/api/customer/paper/balances", headers=headers).status_code == 200
    assert client.get("/api/customer/paper/balances", headers=headers).status_code == 200

    # Reads exhausting their own quota must not consume the separate order quota.
    assert client.post("/api/customer/paper/orders", headers=headers).status_code == 200
    # Equivalent accepted Bearer formatting maps to the same token bucket.
    assert client.post(
        "/api/customer/paper/orders",
        headers={"Authorization": "bearer  stable-token "},
    ).status_code == 429


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
