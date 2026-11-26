from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.schemas import MarketRequest
from app.main import _customer_paper_configuration_ready

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


def test_paper_staging_asgi_exposes_only_explicit_health_auth_customer_and_paper_routes():
    routes = _concrete_dispatch_routes(paper_staging.app)
    assert len(routes) >= 3
    allowed = lambda path: (
        path in {
            "/", "/paper-staging.js", "/paper-staging.css",
            "/healthz", "/livez", "/readyz",
            "/api/auth/signup", "/api/auth/login", "/api/auth/logout",
            "/api/auth/mfa/status", "/api/auth/mfa/enroll",
            "/api/auth/mfa/challenge", "/api/auth/mfa/verify",
            "/api/auth/profile/provision",
            "/api/customer/me", "/api/customer/portfolio",
            "/api/customer/portfolio/equity-history",
            "/api/customer/workspace-preferences",
            "/api/customer/market-scanner",
        }
        or path.startswith("/api/customer/paper/")
    )
    assert all(allowed(route.path) for route in routes)
    assert all(route.path != "/openapi.json" for route in routes)
    assert all(getattr(route, "methods", None) for route in routes), "non-HTTP or mounted route escaped enumeration"
    assert next(r for r in routes if r.path == "/healthz").methods == {"GET"}
    assert next(r for r in routes if r.path == "/livez").methods == {"GET"}
    assert next(r for r in routes if r.path == "/readyz").methods == {"GET"}
    schema = paper_staging.app.openapi()["paths"]
    schema_pairs = {(path, method.upper()) for path, methods in schema.items() for method in methods}
    dispatch_pairs = {(route.path, method) for route in routes for method in route.methods}
    assert dispatch_pairs == schema_pairs, "a dispatchable method is missing from the public route schema"
    assert paper_staging.app.docs_url is None
    assert paper_staging.app.redoc_url is None
    assert paper_staging.app.openapi_url is None



def test_paper_staging_liveness_endpoint_returns_paper_only_status():
    response = TestClient(paper_staging.app).get("/livez")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "paper-only"}


def test_paper_staging_exposes_market_scanner_only_as_customer_read_route():
    routes = _concrete_dispatch_routes(paper_staging.app)
    scanner = next(route for route in routes if route.path == "/api/customer/market-scanner")
    assert scanner.methods == {"GET"}


def test_paper_customer_market_scanner_requires_aal2_and_returns_bounded_public_signals(monkeypatch):
    requests = []
    frames = {"BTC/USDT:USDT": object()}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def fake_customer(authorization, db, *, require_aal2=True):
        assert authorization == "Bearer verified-customer-token"
        assert isinstance(db, FakeSession)
        assert require_aal2 is True
        return SimpleNamespace(id=42), {"sub": "customer-42", "aal": "aal2"}

    def fake_market_data(request):
        requests.append(request)
        return frames[request.symbol]

    async def fake_scan(symbols, *, fetch, metrics, semaphore, logger):
        assert symbols == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
        frame = fetch(symbols[0])
        assert frame is frames[symbols[0]]
        return [{"symbol": symbols[0], **metrics(frame)}]

    monkeypatch.setattr(paper_staging.settings, "paper_staging_market_scanner_enabled", True)
    monkeypatch.setattr(paper_staging, "SessionLocal", FakeSession)
    monkeypatch.setattr(paper_staging, "get_existing_customer", fake_customer)
    monkeypatch.setattr(
        paper_staging,
        "_scanner_normalize_symbols",
        lambda raw: ["BTC/USDT:USDT", "ETH/USDT:USDT"],
    )
    monkeypatch.setattr(paper_staging, "market_data", fake_market_data)
    monkeypatch.setattr(paper_staging, "_scanner_metrics", lambda _frame: {
        "price": 65000.25, "signal": "WATCH"
    })
    monkeypatch.setattr(paper_staging, "_scanner_scan_symbols", fake_scan)

    response = TestClient(paper_staging.app).get(
        "/api/customer/market-scanner?exchange=binance&symbols=BTC%2FUSDT%3AUSDT%2CETH%2FUSDT%3AUSDT",
        headers={"Authorization": "Bearer verified-customer-token"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "exchange": "binance",
        "timeframe": "15m",
        "results": [{
            "symbol": "BTC/USDT:USDT",
            "price": 65000.25,
            "signal": "WATCH",
        }],
    }
    assert len(requests) == 1
    assert requests[0].asset == "crypto"
    assert requests[0].exchange == "binance"
    assert requests[0].timeframe == "15m"
    assert requests[0].days == 30


def test_paper_customer_market_scanner_rejects_customer_without_aal2(monkeypatch):
    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def reject_customer(_authorization, _db, *, require_aal2=True):
        assert require_aal2 is True
        raise HTTPException(403, "AAL2 MFA verification is required for paper trading")

    def unexpected_scan(*_args, **_kwargs):
        raise AssertionError("market data must not be fetched before customer AAL2")

    monkeypatch.setattr(paper_staging.settings, "paper_staging_market_scanner_enabled", True)
    monkeypatch.setattr(paper_staging, "SessionLocal", FakeSession)
    monkeypatch.setattr(paper_staging, "get_existing_customer", reject_customer)
    monkeypatch.setattr(paper_staging, "_scanner_scan_symbols", unexpected_scan)

    response = TestClient(paper_staging.app).get(
        "/api/customer/market-scanner",
        headers={"Authorization": "Bearer aal1-customer-token"},
    )
    assert response.status_code == 403


@pytest.mark.postgres
@pytest.mark.skipif(
    not os.getenv("ATLAS_POSTGRES_URL"),
    reason="requires the disposable PostgreSQL service provisioned by CI",
)
def test_primary_reader_preflight_membership_allowlist_is_behaviorally_exact():
    predicate = paper_staging._UNSAFE_SUPABASE_MANAGED_MEMBERSHIP_PREDICATE

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
                    SELECT count(*) FILTER (WHERE {predicate})::integer AS member_count
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
    assert paper_staging.PaperMarketRequest(days=60).days == 60
    with pytest.raises(ValidationError):
        paper_staging.PaperMarketRequest(days=61)


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


def test_readyz_rate_limits_authenticated_probes_before_database(monkeypatch):
    database_url = (
        "postgresql+asyncpg://atlas_customer_paper:private-test-password@db.example/postgres"
    )
    monkeypatch.setattr(paper_staging.settings, "customer_paper_database_url", database_url)
    token = paper_staging._readiness_bearer_token(database_url)
    calls = []

    def deny_rate_limit(bucket, *, limit, now=None):
        calls.append((bucket, limit))
        return False

    async def forbidden_preflight():
        raise AssertionError("rate-limited readiness must stop before DB preflight")

    monkeypatch.setattr(paper_staging, "_allow_paper_api_request", deny_rate_limit)
    monkeypatch.setattr(
        paper_staging, "_cached_paper_runtime_preflight", forbidden_preflight
    )

    response = TestClient(paper_staging.app).get(
        "/readyz", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert response.json()["reason"] == "READINESS_RATE_LIMIT_EXCEEDED"
    assert len(calls) == 1
    assert calls[0][0].startswith("paper:readyz:")
    assert calls[0][1] == paper_staging._PAPER_READINESS_RATE_LIMIT_PER_MINUTE




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


def test_paper_customer_market_scanner_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(paper_staging.settings, "paper_staging_market_scanner_enabled", False)
    response = TestClient(paper_staging.app).get("/api/customer/market-scanner")
    assert response.status_code == 404


def test_customer_middleware_applies_a_separate_per_customer_market_scanner_quota(monkeypatch):
    paper_staging._paper_api_request_times.clear()
    monkeypatch.setattr(paper_staging, "_PAPER_MARKET_SCANNER_RATE_LIMIT_PER_MINUTE", 1)
    monkeypatch.setattr(paper_staging, "_PAPER_API_IP_RATE_LIMIT_PER_MINUTE", 100)
    monkeypatch.setattr(paper_staging, "_trusted_client_ip", lambda _request: "shared-cloud-run-peer")

    test_app = FastAPI()
    test_app.middleware("http")(paper_staging._paper_customer_api_rate_limit)

    @test_app.get("/api/customer/market-scanner")
    async def market_scanner():
        return {"results": []}

    client = TestClient(test_app)
    headers = {"Authorization": "Bearer stable-market-token"}
    assert client.get("/api/customer/market-scanner", headers=headers).status_code == 200
    limited = client.get(
        "/api/customer/market-scanner",
        headers={"Authorization": "bearer  stable-market-token "},
    )
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"


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
    profile = SimpleNamespace(id=42, auth_user_id="8e9c7e76-f8b6-4caa-96c4-0e451f4e2fb7", status="ACTIVE")

    async def fake_customer_claims(_authorization, require_aal2=True):
        return {"sub": profile.auth_user_id, "aal": "aal2"}

    class QueryResult:
        def __init__(self, row=None, values=()):
            self.row = row
            self.values = values

        def one_or_none(self):
            return self.row

        def scalars(self):
            return SimpleNamespace(all=lambda: list(self.values))

    class ReadOnlyDatabase:
        statements = []

        async def execute(self, query):
            self.statements.append(query)
            selected = {column.name for column in query.selected_columns}
            if selected == {"id", "auth_user_id", "status", "has_stored_email"}:
                return QueryResult(
                    row=SimpleNamespace(
                        id=profile.id,
                        auth_user_id=profile.auth_user_id,
                        status=profile.status,
                        has_stored_email=True,
                    )
                )
            return QueryResult(values=[])

    db = ReadOnlyDatabase()
    monkeypatch.setattr(paper_staging, "customer_claims", fake_customer_claims)

    result_profile, claims = asyncio.run(
        paper_staging.get_existing_customer("Bearer test", db)
    )

    assert result_profile.id == profile.id
    assert result_profile.auth_user_id == profile.auth_user_id
    assert result_profile.status == profile.status
    assert claims["aal"] == "aal2"
    selected = {column.name for column in db.statements[0].selected_columns}
    assert selected == {"id", "auth_user_id", "status", "has_stored_email"}
    assert "email" not in selected and "display_name" not in selected
    assert len(db.statements) == 1


def test_paper_customer_auth_rejects_uuid_profile_while_legacy_auth_references_exist(monkeypatch):
    profile_id = "8e9c7e76-f8b6-4caa-96c4-0e451f4e2fb7"

    async def fake_customer_claims(_authorization, require_aal2=True):
        return {"sub": profile_id, "aal": "aal2"}

    class QueryResult:
        def __init__(self, row=None, values=()):
            self.row = row
            self.values = values

        def one_or_none(self):
            return self.row

        def scalars(self):
            return SimpleNamespace(all=lambda: list(self.values))

    class ReadOnlyDatabase:
        calls = 0

        async def execute(self, _query):
            self.calls += 1
            if self.calls == 1:
                return QueryResult(
                    row=SimpleNamespace(
                        id=42, auth_user_id=profile_id, status="ACTIVE",
                        has_stored_email=False,
                    )
                )
            return QueryResult(values=["legacy-tron-reference"])

    db = ReadOnlyDatabase()
    monkeypatch.setattr(paper_staging, "customer_claims", fake_customer_claims)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(paper_staging.get_existing_customer("Bearer test", db))

    assert exc.value.status_code == 409
    assert "legacy customer identities are manually reviewed" in exc.value.detail
    assert db.calls == 2



def test_pending_paper_identity_does_not_materialize_encrypted_email_without_keyring(monkeypatch):
    auth_id = "8e9c7e76-f8b6-4caa-96c4-0e451f4e2fb7"

    async def fake_customer_claims(_authorization, require_aal2=True):
        return {"sub": auth_id, "aal": "aal2"}

    class Result:
        def one_or_none(self):
            return SimpleNamespace(
                id=42, auth_user_id=auth_id,
                status="PENDING_IDENTITY_REVIEW",
                has_stored_email=False,
            )

        def scalars(self):
            return SimpleNamespace(all=lambda: [])

    class ReadOnlyDatabase:
        statements = []

        async def execute(self, query):
            self.statements.append(query)
            return Result()

    db = ReadOnlyDatabase()
    monkeypatch.setattr(paper_staging, "customer_claims", fake_customer_claims)

    profile, _claims = asyncio.run(
        paper_staging.get_existing_customer(
            "Bearer test", db, allow_identity_review_summary=True
        )
    )

    assert profile.status == "PENDING_IDENTITY_REVIEW"
    assert profile.identity_review_required is True
    selected_names = {column.name for column in db.statements[0].selected_columns}
    assert "email" not in selected_names
    assert "has_stored_email" in selected_names


@pytest.mark.parametrize("flag,enabled", [
    ("live_trading_enabled", True),
    ("customer_live_trading_enabled", True),
    ("payout_live_enabled", True),
    ("usdt_tron_enabled", True),
    ("usdt_tron_sweep_enabled", True),
    ("deriv_live_enabled", True),
    ("forex_live_enabled", True),
    ("binance_arbitrage_live_enabled", True),
    ("broker_sandbox", False),
])
def test_customer_paper_runtime_gate_rejects_each_unsafe_money_path(monkeypatch, flag, enabled):
    settings = paper_staging.settings
    safe_values = {
        "paper_trading": True,
        "live_trading_enabled": False,
        "customer_live_trading_enabled": False,
        "payout_live_enabled": False,
        "usdt_tron_enabled": False,
        "usdt_tron_sweep_enabled": False,
        "deriv_live_enabled": False,
        "forex_live_enabled": False,
        "binance_arbitrage_live_enabled": False,
        "broker_sandbox": True,
        "customer_paper_ledger_enabled": True,
        "customer_paper_database_url": "postgresql+asyncpg://paper:secret@db.example/paper",
    }
    for name, value in safe_values.items():
        monkeypatch.setattr(settings, name, value)
    assert _customer_paper_configuration_ready() is True

    monkeypatch.setattr(settings, flag, enabled)
    assert _customer_paper_configuration_ready() is False


def test_paper_customer_logout_revokes_only_the_authenticated_supabase_session(monkeypatch):
    test_session_token = "-".join(("synthetic", "paper", "session"))
    calls = {"user": [], "logout": []}

    async def fake_supabase_user(token):
        calls["user"].append(token)
        return {"id": "auth-user-42"}

    async def fake_supabase_request(path, payload=None, method="POST", access_token=None):
        calls["logout"].append({
            "path": path,
            "payload": payload,
            "method": method,
            "access_token": access_token,
        })
        return {}

    monkeypatch.setattr(paper_staging, "_supabase_user", fake_supabase_user)
    monkeypatch.setattr(paper_staging, "_supabase_request", fake_supabase_request)
    client = TestClient(paper_staging.app)

    response = client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {test_session_token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "SIGNED_OUT"}
    assert calls["user"] == [test_session_token]
    assert calls["logout"] == [{
        "path": "/auth/v1/logout?scope=local",
        "payload": {},
        "method": "POST",
        "access_token": test_session_token,
    }]

    unauthorized = client.post("/api/auth/logout")
    assert unauthorized.status_code == 401
    assert calls["logout"] == [{
        "path": "/auth/v1/logout?scope=local",
        "payload": {},
        "method": "POST",
        "access_token": test_session_token,
    }]


def test_paper_runtime_preflight_gate_uses_explicit_gate_header(monkeypatch):
    from fastapi import FastAPI

    async def blocked_preflight():
        return {"status": "BLOCKED", "reason": "PAPER_LEDGER_PREFLIGHT_FAILED"}

    monkeypatch.setattr(paper_staging, "_cached_paper_runtime_preflight", blocked_preflight)
    gate_app = FastAPI()

    @gate_app.get("/guard")
    async def guard():
        await paper_staging._paper_state_guard()
        return {"status": "PASS"}

    response = TestClient(gate_app).get("/guard")
    assert response.status_code == 503
    assert response.headers["x-atlas-paper-gate"] == "blocked"
    assert "PAPER_LEDGER_PREFLIGHT_FAILED" in response.json()["detail"]


def _install_paper_scanner_customer(monkeypatch):
    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def existing_customer(_authorization, _db, *, require_aal2=True):
        assert require_aal2 is True
        return SimpleNamespace(id=314), {"sub": "customer-314", "aal": "aal2"}

    monkeypatch.setattr(paper_staging.settings, "paper_staging_market_scanner_enabled", True)
    monkeypatch.setattr(paper_staging, "SessionLocal", FakeSession)
    monkeypatch.setattr(paper_staging, "get_existing_customer", existing_customer)


def test_paper_customer_market_scanner_rejects_fx_symbols_outside_configured_universe(monkeypatch):
    _install_paper_scanner_customer(monkeypatch)
    monkeypatch.setattr(
        paper_staging.settings,
        "adaptive_established_fx_symbols",
        "EUR_USD,GBP_USD,USD_JPY",
        raising=False,
    )

    def unexpected_scan(*_args, **_kwargs):
        pytest.fail("unsupported FX symbol reached the scanner/provider")

    monkeypatch.setattr(paper_staging, "_scanner_scan_symbols", unexpected_scan)
    response = TestClient(paper_staging.app).get(
        "/api/customer/market-scanner?asset_class=forex&symbols=EUR_CHF",
        headers={"Authorization": "Bearer verified-customer-token"},
    )
    assert response.status_code == 422


def test_paper_customer_market_scanner_normalizes_and_deduplicates_configured_fx(monkeypatch):
    _install_paper_scanner_customer(monkeypatch)
    monkeypatch.setattr(
        paper_staging.settings,
        "adaptive_established_fx_symbols",
        "EUR_USD,GBP_USD",
        raising=False,
    )
    monkeypatch.setattr(paper_staging.settings, "forex_broker", "yfinance", raising=False)
    monkeypatch.setattr(paper_staging.settings, "oanda_practice", False, raising=False)
    monkeypatch.setattr(paper_staging.settings, "oanda_account_id", "", raising=False)
    monkeypatch.setattr(paper_staging.settings, "oanda_api_token", "", raising=False)
    fetched = []

    def fetch_fx(symbol, timeframe, days):
        fetched.append((symbol, timeframe, days))
        return object()

    async def scan(symbols, *, fetch, metrics, semaphore, logger):
        assert symbols == ["EUR_USD"]
        fetch(symbols[0])
        return [{"symbol": symbols[0], "price": 1.25, "score": 0.5}]

    monkeypatch.setattr(paper_staging, "fetch_forex", fetch_fx)
    monkeypatch.setattr(paper_staging, "_scanner_scan_symbols", scan)
    response = TestClient(paper_staging.app).get(
        "/api/customer/market-scanner?asset_class=forex&symbols=EUR%2FUSD%2CEUR_USD",
        headers={"Authorization": "Bearer verified-customer-token"},
    )

    assert response.status_code == 200
    assert fetched == [("EUR_USD", "15m", 30)]
    assert response.json()["data_source"] == "yfinance_fx"
    assert response.json()["read_only"] is True


def test_paper_customer_market_scanner_fails_closed_for_commodity_without_oanda_practice(monkeypatch):
    _install_paper_scanner_customer(monkeypatch)
    monkeypatch.setattr(paper_staging.settings, "forex_broker", "oanda", raising=False)
    monkeypatch.setattr(paper_staging.settings, "oanda_practice", False, raising=False)
    monkeypatch.setattr(paper_staging.settings, "oanda_account_id", "practice-account", raising=False)
    monkeypatch.setattr(paper_staging.settings, "oanda_api_token", "practice-token", raising=False)

    def unexpected_scan(*_args, **_kwargs):
        pytest.fail("commodity scanner ran without explicit OANDA practice mode")

    monkeypatch.setattr(paper_staging, "_scanner_scan_symbols", unexpected_scan)
    response = TestClient(paper_staging.app).get(
        "/api/customer/market-scanner?asset_class=commodity&symbols=XAU_USD",
        headers={"Authorization": "Bearer verified-customer-token"},
    )
    assert response.status_code == 503


def test_paper_customer_market_scanner_allows_only_configured_oanda_practice_commodities(monkeypatch):
    _install_paper_scanner_customer(monkeypatch)
    monkeypatch.setattr(
        paper_staging.settings,
        "adaptive_established_commodity_symbols",
        "XAU_USD,WTICO_USD",
        raising=False,
    )
    monkeypatch.setattr(paper_staging.settings, "forex_broker", "oanda", raising=False)
    monkeypatch.setattr(paper_staging.settings, "oanda_practice", True, raising=False)
    monkeypatch.setattr(paper_staging.settings, "oanda_account_id", "practice-account", raising=False)
    monkeypatch.setattr(paper_staging.settings, "oanda_api_token", "practice-token", raising=False)
    fetched = []

    def fetch_oanda(symbol, timeframe, days):
        fetched.append((symbol, timeframe, days))
        return object()

    async def scan(symbols, *, fetch, metrics, semaphore, logger):
        assert symbols == ["XAU_USD", "WTICO_USD"]
        for symbol in symbols:
            fetch(symbol)
        return [{"symbol": symbol, "price": 2000.0, "score": 0.25} for symbol in symbols]

    monkeypatch.setattr(paper_staging, "fetch_forex_oanda", fetch_oanda)
    monkeypatch.setattr(paper_staging, "_scanner_scan_symbols", scan)
    response = TestClient(paper_staging.app).get(
        "/api/customer/market-scanner?asset_class=commodity&symbols=XAUUSD,WTI",
        headers={"Authorization": "Bearer verified-customer-token"},
    )

    assert response.status_code == 200
    assert fetched == [
        ("XAU_USD", "15m", 30),
        ("WTICO_USD", "15m", 30),
    ]
    assert response.json()["data_source"] == "oanda_practice"
    assert response.json()["read_only"] is True


def test_paper_portfolio_records_only_verified_usdt_cash_without_open_positions(monkeypatch):
    async def no_guard():
        return None

    async def existing_customer(_authorization, _db, *, require_aal2=True):
        assert require_aal2 is True
        return SimpleNamespace(id=314), {"sub": "customer-314", "aal": "aal2"}

    async def balances(*, customer_id):
        assert customer_id == 314
        return [{"account_code": "PAPER_CASH", "currency": "USDT", "balance": "105.25"}]

    async def positions(*, customer_id):
        assert customer_id == 314
        return []

    recorded = []

    async def record(**kwargs):
        recorded.append(kwargs)
        return True

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(paper_staging, "SessionLocal", FakeSession)
    monkeypatch.setattr(paper_staging, "get_existing_customer", existing_customer)
    monkeypatch.setattr(paper_staging, "_paper_state_guard", no_guard)
    monkeypatch.setattr(paper_staging, "get_customer_paper_balances", balances)
    monkeypatch.setattr(paper_staging, "get_customer_paper_positions", positions)
    monkeypatch.setattr(paper_staging, "record_customer_paper_equity_snapshot", record)

    response = TestClient(paper_staging.app).get(
        "/api/customer/portfolio",
        headers={"Authorization": "Bearer verified-customer-token"},
    )
    assert response.status_code == 200
    assert response.json()["equity"] == 105.25
    assert response.json()["valuation_status"] == "CASH_ONLY"
    assert response.json()["currency"] == "USDT"
    assert recorded == [{
        "customer_id": 314,
        "equity": Decimal("105.25"),
        "cash_equity": Decimal("105.25"),
        "currency": "USDT",
    }]


def test_paper_portfolio_does_not_record_equity_from_unpriced_open_positions(monkeypatch):
    async def no_guard():
        return None

    async def existing_customer(_authorization, _db, *, require_aal2=True):
        return SimpleNamespace(id=314), {"sub": "customer-314", "aal": "aal2"}

    async def balances(*, customer_id):
        return [{"account_code": "PAPER_CASH", "currency": "USDT", "balance": "105.25"}]

    async def positions(*, customer_id):
        return [{"symbol": "BTC/USDT", "quantity": "0.01"}]

    async def unexpected_record(**_kwargs):
        pytest.fail("equity must not be recorded from stale/unpriced positions")

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(paper_staging, "SessionLocal", FakeSession)
    monkeypatch.setattr(paper_staging, "get_existing_customer", existing_customer)
    monkeypatch.setattr(paper_staging, "_paper_state_guard", no_guard)
    monkeypatch.setattr(paper_staging, "get_customer_paper_balances", balances)
    monkeypatch.setattr(paper_staging, "get_customer_paper_positions", positions)
    monkeypatch.setattr(paper_staging, "record_customer_paper_equity_snapshot", unexpected_record)

    response = TestClient(paper_staging.app).get(
        "/api/customer/portfolio",
        headers={"Authorization": "Bearer verified-customer-token"},
    )
    assert response.status_code == 200
    assert response.json()["equity"] is None
    assert response.json()["valuation_status"] == "CURRENT_MARK_REQUIRED"


def test_paper_portfolio_does_not_record_negative_cash_as_equity(monkeypatch):
    async def no_guard():
        return None

    async def existing_customer(_authorization, _db, *, require_aal2=True):
        return SimpleNamespace(id=314), {"sub": "customer-314", "aal": "aal2"}

    async def balances(*, customer_id):
        return [{"account_code": "PAPER_CASH", "currency": "USDT", "balance": "-0.25"}]

    async def positions(*, customer_id):
        return []

    async def unexpected_record(**_kwargs):
        pytest.fail("negative cash must not be recorded as verified equity")

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(paper_staging, "SessionLocal", FakeSession)
    monkeypatch.setattr(paper_staging, "get_existing_customer", existing_customer)
    monkeypatch.setattr(paper_staging, "_paper_state_guard", no_guard)
    monkeypatch.setattr(paper_staging, "get_customer_paper_balances", balances)
    monkeypatch.setattr(paper_staging, "get_customer_paper_positions", positions)
    monkeypatch.setattr(paper_staging, "record_customer_paper_equity_snapshot", unexpected_record)

    response = TestClient(paper_staging.app).get(
        "/api/customer/portfolio",
        headers={"Authorization": "Bearer verified-customer-token"},
    )
    assert response.status_code == 200
    assert response.json()["equity"] is None
    assert response.json()["valuation_status"] == "CASH_BALANCE_UNAVAILABLE"


def test_paper_equity_history_endpoint_returns_only_persisted_customer_points(monkeypatch):
    async def no_guard():
        return None

    async def existing_customer(_authorization, _db, *, require_aal2=True):
        assert require_aal2 is True
        return SimpleNamespace(id=271), {"sub": "customer-271", "aal": "aal2"}

    async def history(*, customer_id, days):
        assert customer_id == 271
        assert days == 7
        return [{
            "observed_at": "2026-11-26T12:00:00+00:00",
            "equity": 90.5,
            "cash_equity": 90.5,
            "currency": "USDT",
            "valuation_source": "PAPER_LEDGER_CASH_ONLY",
        }]

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(paper_staging, "SessionLocal", FakeSession)
    monkeypatch.setattr(paper_staging, "get_existing_customer", existing_customer)
    monkeypatch.setattr(paper_staging, "_paper_state_guard", no_guard)
    monkeypatch.setattr(paper_staging, "get_customer_paper_equity_history", history)

    response = TestClient(paper_staging.app).get(
        "/api/customer/portfolio/equity-history?days=7",
        headers={"Authorization": "Bearer verified-customer-token"},
    )
    assert response.status_code == 200
    assert response.json()["scope"] == "customer_only"
    assert response.json()["history_complete"] is False
    assert response.json()["points"][0]["valuation_source"] == "PAPER_LEDGER_CASH_ONLY"


def test_paper_workspace_preferences_get_and_patch_use_customer_scoped_storage(monkeypatch):
    async def existing_customer(_authorization, _db, *, require_aal2=True):
        assert require_aal2 is True
        return SimpleNamespace(id=808), {"sub": "customer-808", "aal": "aal2"}

    safe = {
        "reduce_motion": False,
        "compact_markets": False,
        "touch_feedback": True,
        "mobile_chart_crosshair_enabled": True,
        "mobile_default_timeframe_days": 30,
    }
    updated = {**safe, "mobile_default_timeframe_days": 7, "mobile_chart_crosshair_enabled": False}
    calls = []

    async def get_preferences(*, customer_id):
        assert customer_id == 808
        return {"configured": False, "preferences": safe}

    async def update_preferences(*, customer_id, updates):
        calls.append((customer_id, updates))
        return {"configured": True, "preferences": updated}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(paper_staging, "SessionLocal", FakeSession)
    monkeypatch.setattr(paper_staging, "get_existing_customer", existing_customer)
    monkeypatch.setattr(paper_staging, "get_customer_paper_workspace_preferences", get_preferences)
    monkeypatch.setattr(paper_staging, "update_customer_paper_workspace_preferences", update_preferences)

    client = TestClient(paper_staging.app)
    headers = {"Authorization": "Bearer verified-customer-token"}
    loaded = client.get("/api/customer/workspace-preferences", headers=headers)
    saved = client.patch(
        "/api/customer/workspace-preferences",
        headers=headers,
        json={"mobile_default_timeframe_days": 7, "mobile_chart_crosshair_enabled": False},
    )
    assert loaded.status_code == 200
    assert loaded.json()["configured"] is False
    assert loaded.json()["preferences"] == safe
    assert saved.status_code == 200
    assert saved.json()["configured"] is True
    assert saved.json()["preferences"] == updated
    assert calls == [(
        808,
        {"mobile_default_timeframe_days": 7, "mobile_chart_crosshair_enabled": False},
    )]


def test_paper_workspace_preferences_reject_execution_policy_fields(monkeypatch):
    async def existing_customer(_authorization, _db, *, require_aal2=True):
        return SimpleNamespace(id=808), {"sub": "customer-808", "aal": "aal2"}

    async def update_preferences(*, customer_id, updates):
        raise ValueError("only supported display preferences may be changed")

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(paper_staging, "SessionLocal", FakeSession)
    monkeypatch.setattr(paper_staging, "get_existing_customer", existing_customer)
    monkeypatch.setattr(paper_staging, "update_customer_paper_workspace_preferences", update_preferences)

    response = TestClient(paper_staging.app).patch(
        "/api/customer/workspace-preferences",
        headers={"Authorization": "Bearer verified-customer-token"},
        json={"live_trading_enabled": True},
    )
    assert response.status_code == 422


def test_paper_customer_fx_scanner_uses_read_only_yahoo_or_practice_provider(monkeypatch):
    async def existing_customer(_authorization, _db, *, require_aal2=True):
        assert require_aal2 is True
        return SimpleNamespace(id=314), {"sub": "customer-314", "aal": "aal2"}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    fetched = []

    def fetch_fx(symbol, timeframe, days):
        fetched.append((symbol, timeframe, days))
        return object()

    async def scan(symbols, *, fetch, metrics, semaphore, logger):
        for symbol in symbols:
            fetch(symbol)
        return [{"symbol": symbol, "price": 1.25, "score": 0.5} for symbol in symbols]

    monkeypatch.setattr(paper_staging, "SessionLocal", FakeSession)
    monkeypatch.setattr(paper_staging, "get_existing_customer", existing_customer)
    monkeypatch.setattr(paper_staging.settings, "paper_staging_market_scanner_enabled", True)
    monkeypatch.setattr(paper_staging.settings, "forex_broker", "yfinance")
    monkeypatch.setattr(paper_staging.settings, "oanda_practice", False)
    monkeypatch.setattr(paper_staging.settings, "oanda_account_id", "")
    monkeypatch.setattr(paper_staging.settings, "oanda_api_token", "")
    monkeypatch.setattr(paper_staging.settings, "adaptive_established_fx_symbols", "EUR_USD,GBP_USD")
    monkeypatch.setattr(paper_staging, "fetch_forex", fetch_fx)
    monkeypatch.setattr(paper_staging, "_scanner_scan_symbols", scan)

    response = TestClient(paper_staging.app).get(
        "/api/customer/market-scanner?asset_class=forex&symbols=EUR_USD,GBP_USD",
        headers={"Authorization": "Bearer verified-customer-token"},
    )
    assert response.status_code == 200
    assert response.json()["asset_class"] == "forex"
    assert response.json()["data_source"] == "yfinance_fx"
    assert response.json()["read_only"] is True
    assert fetched == [("EUR_USD", "15m", 30), ("GBP_USD", "15m", 30)]


def test_paper_customer_commodity_scanner_fails_closed_without_oanda_practice(monkeypatch):
    async def existing_customer(_authorization, _db, *, require_aal2=True):
        return SimpleNamespace(id=314), {"sub": "customer-314", "aal": "aal2"}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(paper_staging, "SessionLocal", FakeSession)
    monkeypatch.setattr(paper_staging, "get_existing_customer", existing_customer)
    monkeypatch.setattr(paper_staging.settings, "paper_staging_market_scanner_enabled", True)
    monkeypatch.setattr(paper_staging.settings, "forex_broker", "yfinance")
    monkeypatch.setattr(paper_staging.settings, "oanda_practice", False)
    monkeypatch.setattr(paper_staging.settings, "oanda_account_id", "")
    monkeypatch.setattr(paper_staging.settings, "oanda_api_token", "")

    response = TestClient(paper_staging.app).get(
        "/api/customer/market-scanner?asset_class=commodity&symbols=XAU_USD",
        headers={"Authorization": "Bearer verified-customer-token"},
    )
    assert response.status_code == 503
