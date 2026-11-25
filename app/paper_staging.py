"""Paper-only Cloud Run ASGI entrypoint for shared-Supabase staging.

This app deliberately does not run app.main's lifespan: that startup initializes the
general application schema and writes billing state in the primary public schema.
Only explicit health/readiness, Supabase customer Auth, customer summary, read-only
market scanning, and customer-paper ledger routes are exposed by this ASGI app.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field
from sqlalchemy import select, text

from .config import settings
from .body_limit import BodySizeLimitMiddleware
from .monitoring import TelemetryMiddleware
from .db import AppState, CustomerProfile, SessionLocal, engine
from .customer_paper_api import _paper_feature_flags, build_customer_paper_router
from .customer_paper_ledger import (
    get_customer_paper_balances,
    preflight_isolated_customer_paper_ledger,
)
from .customer_paper_lifecycle import get_customer_paper_positions
from .schemas import CustomerCredentials, MfaChallengeRequest, MfaVerifyRequest
# Import only the existing Auth/JWT helpers and market-data adapter. The main ASGI
# app is not mounted or served; its startup/lifespan is never executed in this process.
from .main import (
    MarketRequest, _SCANNER_MAX_CONCURRENT_FETCHES, _scanner_metrics,
    _scanner_normalize_symbols, _scanner_scan_symbols,
    _bearer_token, _customer_mfa_factor_ids, _customer_mfa_state,
    _customer_paper_group_mark_prices,
    _supabase_request, _supabase_user, _trusted_client_ip, api_rate_limit,
    customer_claims, market_data,
)


class PaperMarketRequest(MarketRequest):
    """Bounded market request for isolated paper order submission and historical replay."""

    days: int = Field(default=1, ge=1, le=60)


_EXPECTED_PRIMARY_ROLE = "atlas_paper_reader"
_PAPER_PREFLIGHT_CACHE_TTL_SECONDS = 5.0
_PAPER_API_RATE_WINDOW_SECONDS = 60.0
_PAPER_API_RATE_LIMIT_PER_MINUTE = 60
_PAPER_API_IP_RATE_LIMIT_PER_MINUTE = 600
_PAPER_ORDER_RATE_LIMIT_PER_MINUTE = 12
_PAPER_MARKET_SCANNER_RATE_LIMIT_PER_MINUTE = 12
_PAPER_READINESS_RATE_LIMIT_PER_MINUTE = 30
_PAPER_API_RATE_MAX_BUCKETS = 8192
_paper_preflight_cache_result: dict[str, Any] | None = None
_paper_preflight_cache_at = 0.0
_paper_preflight_cache_lock = asyncio.Lock()
_paper_api_request_times: dict[str, list[float]] = {}
_PAPER_SCANNER_SEMAPHORE = asyncio.Semaphore(_SCANNER_MAX_CONCURRENT_FETCHES)


# Test fixture for the PostgreSQL membership regression tests. The production preflight
# below repeats this allowlist literally so its SQL remains static and scanner-visible.
# The exception is deliberately limited to the one observed Supabase control-plane membership.
_SAFE_SUPABASE_MANAGED_MEMBERSHIP_PREDICATE = """
member_role.rolname='postgres'
AND grantor_role.rolname='supabase_admin'
AND m.admin_option
AND (to_jsonb(m) ? 'inherit_option')
AND COALESCE((to_jsonb(m)->>'inherit_option')::boolean, false)=false
AND (to_jsonb(m) ? 'set_option')
AND COALESCE((to_jsonb(m)->>'set_option')::boolean, false)=false
"""
_UNSAFE_SUPABASE_MANAGED_MEMBERSHIP_PREDICATE = (
    f"NOT ({_SAFE_SUPABASE_MANAGED_MEMBERSHIP_PREDICATE})"
)


def _readiness_bearer_token(paper_database_url: str) -> str:
    """Derive a purpose-bound probe credential from the already-protected paper DB secret."""
    secret = str(paper_database_url or "").strip()
    if not secret:
        return ""
    return hmac.new(
        secret.encode("utf-8"),
        b"atlas-paper-staging-readiness-v1",
        hashlib.sha256,
    ).hexdigest()


def _readiness_token_matches(authorization: str | None, paper_database_url: str) -> bool:
    """Constant-time Bearer-token check using the protected paper DB secret as the HMAC key."""
    expected_token = _readiness_bearer_token(paper_database_url)
    if not expected_token:
        return False
    scheme, separator, supplied = str(authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not supplied.strip():
        return False
    return hmac.compare_digest(supplied.strip().encode("utf-8"), expected_token.encode("utf-8"))


async def _cached_paper_runtime_preflight() -> dict[str, Any]:
    """Cache expensive role/schema checks briefly; risk state is still queried per request."""
    global _paper_preflight_cache_result, _paper_preflight_cache_at
    now = time.monotonic()
    if (
        _paper_preflight_cache_result is not None
        and now - _paper_preflight_cache_at < _PAPER_PREFLIGHT_CACHE_TTL_SECONDS
    ):
        return dict(_paper_preflight_cache_result)
    async with _paper_preflight_cache_lock:
        now = time.monotonic()
        if (
            _paper_preflight_cache_result is not None
            and now - _paper_preflight_cache_at < _PAPER_PREFLIGHT_CACHE_TTL_SECONDS
        ):
            return dict(_paper_preflight_cache_result)
        result = await _paper_runtime_preflight()
        _paper_preflight_cache_result = dict(result)
        _paper_preflight_cache_at = time.monotonic()
        return dict(result)


def _allow_paper_api_request(bucket_key: str, *, limit: int, now: float | None = None) -> bool:
    """Enforce per-IP and per-token request limits without Redis, with bounded state."""
    current = time.monotonic() if now is None else float(now)
    key = str(bucket_key or "").strip()[:180]
    if not key or limit < 1:
        return False

    if key not in _paper_api_request_times and len(_paper_api_request_times) >= _PAPER_API_RATE_MAX_BUCKETS:
        expired = [
            existing for existing, entries in _paper_api_request_times.items()
            if not any(0 <= current - seen < _PAPER_API_RATE_WINDOW_SECONDS for seen in entries)
        ]
        for existing in expired:
            _paper_api_request_times.pop(existing, None)
        if len(_paper_api_request_times) >= _PAPER_API_RATE_MAX_BUCKETS:
            return False

    recent = [
        seen for seen in _paper_api_request_times.get(key, [])
        if 0 <= current - seen < _PAPER_API_RATE_WINDOW_SECONDS
    ]
    if len(recent) >= limit:
        _paper_api_request_times[key] = recent
        return False
    recent.append(current)
    _paper_api_request_times[key] = recent
    return True


def _primary_reader_membership_gate_passes(member_count: Any) -> bool:
    """Only zero non-allowlisted incoming memberships may pass primary-reader preflight."""
    try:
        return int(member_count) == 0
    except (TypeError, ValueError):
        return False


async def _primary_reader_preflight() -> dict[str, Any]:
    """Permit only read-only access to the two existing tables required for paper auth/kill switch."""
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT current_database(), current_user, session_user,
                       current_setting('transaction_read_only') AS transaction_read_only,
                       r.rolcanlogin, r.rolsuper, r.rolcreaterole, r.rolcreatedb,
                       r.rolreplication, r.rolbypassrls, r.rolinherit,
                       (SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid) AS memberships,
                       (SELECT count(*) FROM pg_auth_members m
                         JOIN pg_roles member_role ON member_role.oid=m.member
                         JOIN pg_roles grantor_role ON grantor_role.oid=m.grantor
                         WHERE m.roleid=r.oid
                           AND NOT (
                             member_role.rolname='postgres'
                             AND grantor_role.rolname='supabase_admin'
                             AND m.admin_option
                             AND (to_jsonb(m) ? 'inherit_option')
                             AND COALESCE((to_jsonb(m)->>'inherit_option')::boolean, false)=false
                             AND (to_jsonb(m) ? 'set_option')
                             AND COALESCE((to_jsonb(m)->>'set_option')::boolean, false)=false
                           )
                       ) AS member_count,
                       has_schema_privilege(current_user, 'public', 'USAGE') AS public_usage,
                       has_schema_privilege(current_user, 'public', 'CREATE') AS public_create,
                       (SELECT count(*) FROM pg_namespace n
                         WHERE n.nspname NOT LIKE 'pg_%'
                           AND n.nspname <> 'information_schema'
                           AND n.nspname <> 'public'
                           AND (has_schema_privilege(current_user, n.oid, 'USAGE')
                             OR has_schema_privilege(current_user, n.oid, 'CREATE'))) AS other_schema_privileges,
                       has_table_privilege(current_user, 'public.customer_profiles', 'SELECT') AS can_read_profiles,
                       has_table_privilege(current_user, 'public.app_state', 'SELECT') AS can_read_app_state,
                       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                         WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f')
                           AND (
                             (c.relname IN ('customer_profiles','app_state') AND (
                               has_table_privilege(current_user,c.oid,'INSERT')
                               OR has_table_privilege(current_user,c.oid,'UPDATE')
                               OR has_table_privilege(current_user,c.oid,'DELETE')
                               OR has_table_privilege(current_user,c.oid,'TRUNCATE')
                               OR has_table_privilege(current_user,c.oid,'REFERENCES')
                               OR has_table_privilege(current_user,c.oid,'TRIGGER')
                             ))
                             OR (c.relname NOT IN ('customer_profiles','app_state') AND (
                               has_table_privilege(current_user,c.oid,'SELECT')
                               OR has_table_privilege(current_user,c.oid,'INSERT')
                               OR has_table_privilege(current_user,c.oid,'UPDATE')
                               OR has_table_privilege(current_user,c.oid,'DELETE')
                               OR has_table_privilege(current_user,c.oid,'TRUNCATE')
                               OR has_table_privilege(current_user,c.oid,'REFERENCES')
                               OR has_table_privilege(current_user,c.oid,'TRIGGER')
                             ))
                           )) AS unexpected_public_table_privileges,
                       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                         WHERE n.nspname='public' AND c.relkind='S'
                           AND (has_sequence_privilege(current_user,c.oid,'USAGE')
                             OR has_sequence_privilege(current_user,c.oid,'SELECT')
                             OR has_sequence_privilege(current_user,c.oid,'UPDATE'))) AS public_sequence_privileges,
                       (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                         WHERE n.nspname='public' AND p.prosecdef
                           AND has_function_privilege(current_user,p.oid,'EXECUTE')) AS callable_public_security_definers,
                       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                         WHERE n.nspname='public' AND c.relkind IN ('r','p')
                           AND c.relowner = r.oid) AS owned_public_tables
                FROM pg_roles r WHERE r.rolname=current_user
            """))).mappings().one()
    except Exception:
        return {"status": "BLOCKED", "reason": "PRIMARY_READ_ONLY_IDENTITY_UNAVAILABLE"}

    observed_role = str(row["current_user"] or "")
    if (
        str(row["current_database"] or "") != "postgres"
        or str(row["session_user"] or "") != observed_role
        or str(row["transaction_read_only"] or "").lower() not in {"on", "true", "t"}
        or observed_role != _EXPECTED_PRIMARY_ROLE
        or not bool(row["rolcanlogin"])
        or bool(row["rolsuper"])
        or bool(row["rolcreaterole"])
        or bool(row["rolcreatedb"])
        or bool(row["rolreplication"])
        or not bool(row["rolbypassrls"])
        or bool(row["rolinherit"])
        or int(row["memberships"]) != 0
        or not _primary_reader_membership_gate_passes(row["member_count"])
        or not bool(row["public_usage"])
        or bool(row["public_create"])
        or int(row["other_schema_privileges"]) != 0
        or not bool(row["can_read_profiles"])
        or not bool(row["can_read_app_state"])
        or int(row["unexpected_public_table_privileges"]) != 0
        or int(row["public_sequence_privileges"]) != 0
        or int(row["callable_public_security_definers"]) != 0
        or int(row["owned_public_tables"]) != 0
    ):
        return {"status": "BLOCKED", "reason": "PRIMARY_DATABASE_ROLE_PRIVILEGES_UNSAFE"}

    return {
        "status": "PASS",
        "primary_role": observed_role,
        "primary_access": "SELECT_ONLY_CUSTOMER_PROFILES_AND_APP_STATE",
        "primary_public_schema_writes": False,
    }


async def _paper_runtime_preflight() -> dict[str, Any]:
    """All independent gates must pass before this service accepts any paper request."""
    if not bool(getattr(settings, "customer_totp_required", True)):
        return {"status": "BLOCKED", "reason": "CUSTOMER_TOTP_REQUIREMENT_DISABLED"}
    try:
        _paper_feature_flags()
    except HTTPException:
        return {"status": "BLOCKED", "reason": "PAPER_ONLY_SAFETY_FLAGS_NOT_READY"}

    primary = await _primary_reader_preflight()
    if primary.get("status") != "PASS":
        return primary

    ledger = await preflight_isolated_customer_paper_ledger(require_enabled=True)
    if not isinstance(ledger, dict) or str(ledger.get("status") or "").upper() != "PASS":
        reason = str(ledger.get("reason") or "CUSTOMER_PAPER_LEDGER_PREFLIGHT_FAILED") if isinstance(ledger, dict) else "CUSTOMER_PAPER_LEDGER_PREFLIGHT_INVALID"
        return {"status": "BLOCKED", "reason": reason}
    return {
        "status": "PASS",
        "mode": "paper-only",
        "primary": primary,
        "paper_ledger": {
            "status": "PASS",
            "isolation_mode": ledger.get("isolation_mode", "shared_database_schema"),
        },
    }


async def _has_unmapped_legacy_customer_profile(db, auth_user_id: str) -> bool:
    """Check for one non-UUID legacy identity without materializing the profile table."""
    bind_getter = getattr(db, "get_bind", None)
    bind = bind_getter() if callable(bind_getter) else getattr(db, "bind", None)
    dialect = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect == "postgresql":
        uuid_pattern = r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$"
        statement = select(CustomerProfile.id).where(
            CustomerProfile.auth_user_id != auth_user_id,
            CustomerProfile.auth_user_id.op("!~*")(uuid_pattern),
        ).limit(1)
        return (await db.execute(statement)).scalar_one_or_none() is not None
    if dialect == "sqlite":
        uuid_glob = (
            "[0-9a-fA-F]" * 8 + "-"
            + "[0-9a-fA-F]" * 4 + "-"
            + "[0-9a-fA-F]" * 4 + "-"
            + "[0-9a-fA-F]" * 4 + "-"
            + "[0-9a-fA-F]" * 12
        )
        statement = select(CustomerProfile.id).where(
            CustomerProfile.auth_user_id != auth_user_id,
            ~CustomerProfile.auth_user_id.op("GLOB")(uuid_glob),
        ).limit(1)
        return (await db.execute(statement)).scalar_one_or_none() is not None
    # Unknown dialects retain a conservative compatibility path.
    legacy_refs = (await db.execute(
        select(CustomerProfile.auth_user_id).where(CustomerProfile.auth_user_id != auth_user_id)
    )).scalars().all()
    for reference in legacy_refs:
        try:
            uuid.UUID(str(reference))
        except (ValueError, TypeError, AttributeError):
            return True
    return False


async def get_existing_customer(
    authorization: str | None,
    db,
    require_aal2: bool = True,
    allow_identity_review_summary: bool = False,
):
    """Verify MFA and an already-provisioned customer using SELECTs only; never self-register."""
    claims = await customer_claims(authorization, require_aal2=require_aal2)
    if str(claims.get("aal") or "aal1") != "aal2":
        raise HTTPException(403, "AAL2 MFA verification is required for paper trading")
    uid = str(claims.get("sub") or "").strip()
    if not uid:
        raise HTTPException(401, "Customer authentication is invalid")
    row = (
        await db.execute(
            select(
                CustomerProfile.id,
                CustomerProfile.auth_user_id,
                CustomerProfile.status,
                (
                    ~(CustomerProfile.email.is_(None) | (CustomerProfile.email == ""))
                ).label("has_stored_email"),
            ).where(CustomerProfile.auth_user_id == uid)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            403,
            "Customer profile must already exist before paper-only staging access",
        )
    # Never select/decrypt email in this keyless service. SQL returns only a
    # boolean presence expression; ciphertext is not materialized by the ORM.
    # Pending identities remain pending until the key-bearing customer service
    # makes a verified decision. Active profiles with no stored email fail closed
    # when any unmapped legacy Auth identity remains.
    status = str(row.status or "").upper()
    has_stored_email = bool(getattr(row, "has_stored_email", False))
    identity_review_required = status == "PENDING_IDENTITY_REVIEW"
    if status == "ACTIVE" and not has_stored_email:
        identity_review_required = await _has_unmapped_legacy_customer_profile(db, uid)
    effective_status = row.status
    if identity_review_required and not allow_identity_review_summary:
        raise HTTPException(
            409,
            "Paper access is paused until legacy customer identities are manually reviewed. Contact support.",
            headers={"X-Atlas-Paper-Gate": "blocked"},
        )

    # Keep PII/encrypted columns out of the query: this service has no encryption
    # keyring and only needs the numeric tenant ID and active status.
    profile = SimpleNamespace(
        id=row.id,
        auth_user_id=row.auth_user_id,
        status=effective_status,
        identity_review_required=identity_review_required,
    )
    if str(getattr(profile, "status", "")).upper() != "ACTIVE" and not (
        allow_identity_review_summary
        and identity_review_required
        and str(getattr(profile, "status", "")).upper() == "PENDING_IDENTITY_REVIEW"
    ):
        raise HTTPException(403, "Customer account is not active")
    return profile, claims


async def _paper_state_guard() -> None:
    """Check database and paper-ledger gates, then honor the persisted kill switch."""
    result = await _cached_paper_runtime_preflight()
    gate_headers = {"X-Atlas-Paper-Gate": "blocked"}
    if result.get("status") != "PASS":
        raise HTTPException(
            503,
            "Paper-only staging preflight is blocked: " + str(result.get("reason") or "UNKNOWN"),
            headers=gate_headers,
        )
    async with SessionLocal() as db:
        state = await db.get(AppState, 1)
    if state is None:
        raise HTTPException(
            503,
            "Paper-only staging risk state has not been provisioned",
            headers=gate_headers,
        )
    if state.kill_switch:
        raise HTTPException(
            409,
            "Customer paper trading is halted by platform risk state",
            headers=gate_headers,
        )


app = FastAPI(
    title="Atlas Paper-Only Staging API",
    version=settings.app_version,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

_PAPER_STAGING_UI_DIR = Path(__file__).resolve().parent / "paper_staging_ui"


@app.get("/", include_in_schema=True)
async def paper_staging_home():
    """Serve the dedicated browser console for the restricted paper-only API."""
    return FileResponse(
        _PAPER_STAGING_UI_DIR / "index.html",
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/paper-staging.js", include_in_schema=True)
async def paper_staging_javascript():
    """Serve same-origin browser code under the staging Content Security Policy."""
    return FileResponse(
        _PAPER_STAGING_UI_DIR / "paper-staging.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/paper-staging.css", include_in_schema=True)
async def paper_staging_stylesheet():
    """Serve the small responsive stylesheet for the paper testing console."""
    return FileResponse(
        _PAPER_STAGING_UI_DIR / "paper-staging.css",
        media_type="text/css; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


# Reject oversized customer-paper request bodies before auth/route handlers buffer them.
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)

# Preserve the existing per-IP plus per-authenticated-identity rate limiter without
# mounting the main application's production routes or lifespan.
app.middleware("http")(api_rate_limit)


@app.middleware("http")
async def _paper_customer_api_rate_limit(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/customer/paper/") or path == "/api/customer/market-scanner":
        if path == "/api/customer/market-scanner":
            request_class = "market"
        else:
            request_class = (
                "order"
                if request.method.upper() == "POST" and "/orders" in path
                else "general"
            )
        raw_authorization = str(request.headers.get("authorization") or "").strip()
        if raw_authorization.lower().startswith("bearer "):
            # Match _bearer_token exactly: case-insensitive scheme, split once, trim token.
            normalized_token = raw_authorization.split(" ", 1)[1].strip()
            authorization_id = hashlib.sha256(normalized_token.encode("utf-8")).hexdigest()[:24] if normalized_token else "anonymous"
        else:
            authorization_id = "anonymous"
        ip_bucket = f"paper:ip:{_trusted_client_ip(request)}:{request_class}"
        identity_bucket = f"paper:auth:{authorization_id}:{request_class}"
        limit = (
            _PAPER_ORDER_RATE_LIMIT_PER_MINUTE
            if request_class == "order"
            else _PAPER_MARKET_SCANNER_RATE_LIMIT_PER_MINUTE
            if request_class == "market"
            else _PAPER_API_RATE_LIMIT_PER_MINUTE
        )
        if not _allow_paper_api_request(
            ip_bucket, limit=_PAPER_API_IP_RATE_LIMIT_PER_MINUTE
        ) or not _allow_paper_api_request(
            identity_bucket, limit=limit
        ):
            return JSONResponse(
                {"detail": "Rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": "60"},
            )
    return await call_next(request)


# Keep telemetry outermost so 413 and 429 responses receive the security headers,
# request ID, and metrics used by the main application.
app.add_middleware(TelemetryMiddleware)


@app.get("/healthz")
@app.get("/livez")
async def healthz():
    # Liveness only: don't make the container restart merely because provisioning is pending.
    return {"status": "ok", "mode": "paper-only"}


@app.get("/readyz")
async def readyz(request: Request):
    # Cloud Run is public for liveness and customer routes, but readiness is a
    # release-gate endpoint. Reject unauthenticated probes before touching Supabase.
    paper_database_url = str(getattr(settings, "customer_paper_database_url", "") or "")
    if not paper_database_url:
        return JSONResponse(
            {"status": "BLOCKED", "reason": "PAPER_DATABASE_CREDENTIAL_NOT_CONFIGURED"},
            status_code=503,
        )
    if not _readiness_token_matches(request.headers.get("authorization"), paper_database_url):
        return JSONResponse(
            {"status": "BLOCKED", "reason": "READINESS_AUTHENTICATION_REQUIRED"},
            status_code=401,
        )

    # Only authenticated readiness probes consume this bucket. This prevents an
    # unauthenticated caller from exhausting the operator's readiness quota, while
    # bounding valid callers before each probe opens the shared Supabase session.
    readiness_bucket = f"paper:readyz:{_trusted_client_ip(request)}"
    if not _allow_paper_api_request(
        readiness_bucket, limit=_PAPER_READINESS_RATE_LIMIT_PER_MINUTE
    ):
        return JSONResponse(
            {"status": "BLOCKED", "reason": "READINESS_RATE_LIMIT_EXCEEDED"},
            status_code=429,
            headers={"Retry-After": "60"},
        )

    result = await _cached_paper_runtime_preflight()
    if result.get("status") != "PASS":
        return JSONResponse(result, status_code=503)
    async with SessionLocal() as db:
        state = await db.get(AppState, 1)
    if state is None:
        return JSONResponse({"status": "BLOCKED", "reason": "RISK_STATE_NOT_PROVISIONED"}, status_code=503)
    if state.kill_switch:
        return JSONResponse({"status": "BLOCKED", "reason": "PAPER_TRADING_HALTED"}, status_code=503)
    return result


@app.post("/api/auth/signup")
async def paper_customer_signup(req: CustomerCredentials):
    """Create a Supabase Auth identity; email confirmation remains controlled by Auth."""
    return await _supabase_request(
        "/auth/v1/signup",
        payload={"email": req.email.strip().lower(), "password": req.password},
    )


@app.post("/api/auth/login")
async def paper_customer_login(req: CustomerCredentials):
    """Authenticate the first factor before the TOTP enrollment/challenge step."""
    return await _supabase_request(
        "/auth/v1/token?grant_type=password",
        payload={"email": req.email.strip().lower(), "password": req.password},
    )


@app.post("/api/auth/logout")
async def paper_customer_logout(authorization: str | None = Header(default=None)):
    """Revoke the caller's current Supabase Auth session during browser sign-out."""
    token = _bearer_token(authorization)
    # Resolve the user first so this endpoint cannot be used as an unauthenticated
    # Auth-service relay. Supabase invalidates the session associated with this JWT.
    await _supabase_user(token)
    await _supabase_request(
        "/auth/v1/logout",
        payload={},
        access_token=token,
    )
    return {"status": "SIGNED_OUT"}


@app.get("/api/auth/mfa/status")
async def paper_customer_mfa_status(authorization: str | None = Header(default=None)):
    state = await _customer_mfa_state(authorization)
    return {
        "enabled": state["enabled"],
        "aal": state["aal"],
        "factor_count": state["factor_count"],
        "factor_ids": state["factor_ids"],
    }


@app.post("/api/auth/mfa/enroll")
async def paper_customer_mfa_enroll(authorization: str | None = Header(default=None)):
    await customer_claims(authorization, require_aal2=False)
    state = await _customer_mfa_state(authorization)
    if state["enabled"]:
        return {
            "enabled": True,
            "factor_ids": state["factor_ids"],
            "message": "A verified authenticator is already enrolled.",
        }
    token = _bearer_token(authorization)
    user = await _supabase_user(token)
    friendly_name = "Atlas Paper Trading / Google Authenticator"
    # Supabase rejects re-enrollment when an abandoned setup leaves an unverified
    # factor with the same friendly name. Its original setup secret cannot be
    # recovered, so remove only this user's matching stale pending TOTP factor.
    for factor in user.get("factors") or []:
        if (
            str(factor.get("factor_type") or "").lower() != "totp"
            or str(factor.get("status") or "").lower() != "unverified"
            or str(factor.get("friendly_name") or "") != friendly_name
        ):
            continue
        try:
            factor_id = str(uuid.UUID(str(factor.get("id") or "")))
        except (ValueError, TypeError, AttributeError):
            continue
        try:
            await _supabase_request(
                f"/auth/v1/factors/{factor_id}",
                method="DELETE",
                access_token=token,
            )
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
    return await _supabase_request(
        "/auth/v1/factors",
        payload={"factor_type": "totp", "friendly_name": friendly_name},
        access_token=token,
    )


@app.post("/api/auth/mfa/challenge")
async def paper_customer_mfa_challenge(
    req: MfaChallengeRequest, authorization: str | None = Header(default=None)
):
    await customer_claims(authorization, require_aal2=False)
    factor_ids = await _customer_mfa_factor_ids(authorization)
    if req.factor_id not in factor_ids:
        raise HTTPException(403, "Invalid authenticator factor")
    return await _supabase_request(
        f"/auth/v1/factors/{req.factor_id}/challenge",
        payload={},
        access_token=_bearer_token(authorization),
    )


@app.post("/api/auth/mfa/verify")
async def paper_customer_mfa_verify(
    req: MfaVerifyRequest, authorization: str | None = Header(default=None)
):
    await customer_claims(authorization, require_aal2=False)
    factor_ids = await _customer_mfa_factor_ids(authorization)
    if req.factor_id not in factor_ids:
        raise HTTPException(403, "Invalid authenticator factor")
    return await _supabase_request(
        f"/auth/v1/factors/{req.factor_id}/verify",
        payload={"challenge_id": req.challenge_id, "code": req.code},
        access_token=_bearer_token(authorization),
    )


@app.post("/api/auth/profile/provision")
async def paper_customer_profile_provision(authorization: str | None = Header(default=None)):
    """Provision only the caller's profile after email confirmation and TOTP AAL2."""
    await customer_claims(authorization, require_aal2=True)
    token = _bearer_token(authorization)
    user = await _supabase_user(token)
    if not user.get("email_confirmed_at"):
        raise HTTPException(403, "A confirmed email address is required")
    return await _supabase_request(
        "/functions/v1/customer-profile-provision",
        payload={},
        access_token=token,
    )


@app.get("/api/customer/me")
async def paper_customer_me(authorization: str | None = Header(default=None)):
    async with SessionLocal() as db:
        # Summary is read-only: show the authenticated user's own Auth PII and
        # an explicit review flag without granting paper-ledger access.
        profile, claims = await get_existing_customer(
            authorization, db, allow_identity_review_summary=True
        )
    # The paper-staging runtime has a read-only identity role and deliberately
    # no application encryption keyring. Do not select EncryptedText columns
    # from customer_profiles; resolve display PII from the verified Auth user.
    user = await _supabase_user(_bearer_token(authorization))
    uid = str(claims.get("sub") or "").strip()
    if not uid or user.get("id") != uid:
        raise HTTPException(401, "Customer Auth identity could not be verified")
    if not user.get("email_confirmed_at"):
        raise HTTPException(403, "A confirmed email address is required")
    metadata = user.get("user_metadata") or {}
    return {
        "id": profile.id,
        "email": str(user.get("email") or ""),
        "display_name": str(metadata.get("full_name") or metadata.get("name") or "")[:160],
        "status": profile.status,
        "identity_review_required": bool(profile.identity_review_required),
    }


@app.get("/api/customer/market-scanner")
async def paper_customer_market_scanner(
    exchange: str = "binance",
    symbols: str = "BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT",
    authorization: str | None = Header(default=None),
):
    """Serve bounded, read-only public market signals to authenticated paper customers."""
    # The shared Supabase paper role cannot read subscription entitlements. Keep
    # this scanner disabled by default and expose it only through the explicit
    # staging-only service flag; the full API retains its per-plan feature gate.
    if not settings.paper_staging_market_scanner_enabled:
        raise HTTPException(404, "Market scanner is not enabled in this environment")

    async with SessionLocal() as db:
        # Market data is customer-visible only after the same AAL2 profile check as
        # the paper portfolio; this route never creates or advances an order.
        await get_existing_customer(authorization, db, require_aal2=True)

    import ccxt

    if exchange not in ccxt.exchanges:
        raise HTTPException(422, "Unsupported exchange")

    requested = _scanner_normalize_symbols(symbols)
    if not requested:
        raise HTTPException(422, "At least one valid market symbol is required")

    results = await _scanner_scan_symbols(
        requested,
        fetch=lambda symbol: market_data(
            MarketRequest(
                asset="crypto",
                symbol=symbol,
                exchange=exchange,
                timeframe="15m",
                days=30,
            )
        ),
        metrics=_scanner_metrics,
        semaphore=_PAPER_SCANNER_SEMAPHORE,
        logger=logging.getLogger(__name__),
    )
    results.sort(
        key=lambda row: abs(float(row.get("score", 0.0) or 0.0)),
        reverse=True,
    )
    return {"exchange": exchange, "timeframe": "15m", "results": results}


@app.get("/api/customer/portfolio")
async def paper_customer_portfolio(authorization: str | None = Header(default=None)):
    # Authenticate first so unauthenticated callers cannot probe preflight or
    # kill-switch state. Keep the paper gate before any balance/portfolio read.
    async with SessionLocal() as db:
        profile, _claims = await get_existing_customer(authorization, db)
    await _paper_state_guard()
    balances = await get_customer_paper_balances(customer_id=int(profile.id))
    positions = await get_customer_paper_positions(customer_id=int(profile.id))
    by_code = {
        str(row.get("account_code") or ""): Decimal(str(row.get("balance") or "0"))
        for row in balances
    }
    cash = by_code.get("PAPER_CASH")
    # Open positions require a current, verified mark to calculate total equity.
    # Never substitute cost basis or a stale value and label it as market equity.
    equity = (
        float(cash)
        if cash is not None and not positions
        else None
    )
    return {
        "equity": equity,
        "cash_equity": float(cash) if cash is not None else None,
        "positions": positions,
        "virtual_only": True,
        "valuation_status": "CASH_ONLY" if not positions else "CURRENT_MARK_REQUIRED",
    }


app.include_router(
    build_customer_paper_router(
        get_customer=get_existing_customer,
        market_data=market_data,
        market_request_type=PaperMarketRequest,
        state_guard=_paper_state_guard,
        group_mark_price_loader=_customer_paper_group_mark_prices,
    )
)
