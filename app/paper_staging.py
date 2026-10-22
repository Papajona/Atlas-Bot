"""Paper-only Cloud Run ASGI entrypoint for shared-Supabase staging.

This app deliberately does not run app.main's lifespan: that startup initializes the
general application schema and writes billing state in the primary public schema.
Only health/readiness and /api/customer/paper are exposed by this ASGI app.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import select, text

from .config import settings
from .db import AppState, CustomerProfile, SessionLocal, engine
from .customer_paper_api import _paper_feature_flags, build_customer_paper_router
from .customer_paper_ledger import preflight_isolated_customer_paper_ledger
# Import the existing JWT/MFA verifier and market-data adapter. The main ASGI app is
# not mounted or served; its startup/lifespan is never executed in this process.
from .main import MarketRequest, _trusted_client_ip, api_rate_limit, customer_claims, market_data


class PaperMarketRequest(MarketRequest):
    """Restricted short-window market request used only by the isolated paper runtime."""

    days: int = Field(default=1, ge=1, le=30)


_EXPECTED_PRIMARY_ROLE = "atlas_paper_reader"
_PAPER_PREFLIGHT_CACHE_TTL_SECONDS = 5.0
_PAPER_API_RATE_WINDOW_SECONDS = 60.0
_PAPER_API_RATE_LIMIT_PER_MINUTE = 60
_PAPER_API_IP_RATE_LIMIT_PER_MINUTE = 600
_PAPER_ORDER_RATE_LIMIT_PER_MINUTE = 12
_PAPER_API_RATE_MAX_BUCKETS = 8192
_paper_preflight_cache_result: dict[str, Any] | None = None
_paper_preflight_cache_at = 0.0
_paper_preflight_cache_lock = asyncio.Lock()
_paper_api_request_times: dict[str, list[float]] = {}


# This exact predicate is used by the live read-only preflight and exercised against
# PostgreSQL by the membership regression tests below. The exception is deliberately
# limited to the one observed Supabase control-plane membership.
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
            row = (await conn.execute(text(f"""
                SELECT current_database(), current_user, session_user,
                       current_setting('transaction_read_only') AS transaction_read_only,
                       r.rolcanlogin, r.rolsuper, r.rolcreaterole, r.rolcreatedb,
                       r.rolreplication, r.rolbypassrls, r.rolinherit,
                       (SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid) AS memberships,
                       (SELECT count(*) FROM pg_auth_members m
                         JOIN pg_roles member_role ON member_role.oid=m.member
                         JOIN pg_roles grantor_role ON grantor_role.oid=m.grantor
                         WHERE m.roleid=r.oid
                           AND {_UNSAFE_SUPABASE_MANAGED_MEMBERSHIP_PREDICATE}
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


async def get_existing_customer(
    authorization: str | None, db, require_aal2: bool = True
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
            ).where(CustomerProfile.auth_user_id == uid)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            403,
            "Customer profile must already exist before paper-only staging access",
        )
    # Keep PII/encrypted columns out of the query: this service has no encryption
    # keyring and only needs the numeric tenant ID and active status.
    profile = SimpleNamespace(
        id=row.id,
        auth_user_id=row.auth_user_id,
        status=row.status,
    )
    if str(getattr(profile, "status", "")).upper() != "ACTIVE":
        raise HTTPException(403, "Customer account is not active")
    return profile, claims


async def _paper_state_guard() -> None:
    """Check database and paper-ledger gates, then honor the persisted kill switch."""
    result = await _cached_paper_runtime_preflight()
    if result.get("status") != "PASS":
        raise HTTPException(503, "Paper-only staging preflight is blocked: " + str(result.get("reason") or "UNKNOWN"))
    async with SessionLocal() as db:
        state = await db.get(AppState, 1)
    if state is None:
        raise HTTPException(503, "Paper-only staging risk state has not been provisioned")
    if state.kill_switch:
        raise HTTPException(409, "Customer paper trading is halted by platform risk state")


app = FastAPI(
    title="Atlas Paper-Only Staging API",
    version=settings.app_version,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Preserve the existing per-IP plus per-authenticated-identity rate limiter without
# mounting the main application's production routes or lifespan.
app.middleware("http")(api_rate_limit)


@app.middleware("http")
async def _paper_customer_api_rate_limit(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/customer/paper/"):
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


@app.get("/healthz")
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


app.include_router(
    build_customer_paper_router(
        get_customer=get_existing_customer,
        market_data=market_data,
        market_request_type=PaperMarketRequest,
        state_guard=_paper_state_guard,
    )
)
