#!/usr/bin/env python3
"""Read-only preflight for a paper-only test run.

Run it with the same environment the app will use, BEFORE a paper drill:

    python scripts/paper_preflight.py            # configuration checks only
    python scripts/paper_preflight.py --check-db # also checks database state (read-only)

Exit status is 1 when any check FAILs (2 with --strict when any check WARNs), else 0. The script
never writes, never places orders and never prints secrets or database passwords.

Every check mirrors something the application itself enforces (Settings validators, the /readyz
checks, ``configured_paper_execution_settings``); it does not invent additional policy.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
Result = tuple[str, str, str]  # (status, check, detail)

# Money paths that must all be off for a paper-only run: (setting, required value).
_MUST_BE_OFF = (
    "live_trading_enabled",
    "customer_live_trading_enabled",
    "payout_live_enabled",
    "usdt_tron_enabled",
    "usdt_tron_sweep_enabled",
    "deriv_live_enabled",
    "forex_live_enabled",
    "binance_arbitrage_live_enabled",
)


def _s(settings: Any, name: str, default: Any = None) -> Any:
    return getattr(settings, name, default)


def _text(value: Any) -> str:
    return str(value or "").strip()


def check_settings(settings: Any, *, encryption_check=None, calibration_check=None) -> list[Result]:
    """Pure configuration checks. ``settings`` is any object with the app's setting names."""
    out: list[Result] = []
    env = _text(_s(settings, "environment"))
    non_dev = env in {"staging", "production"}

    # 1. Money paths. These are the only checks that should ever block a paper run outright.
    if _s(settings, "paper_trading") is not True:
        out.append((FAIL, "paper_trading", "PAPER_TRADING must be true for a paper-only run"))
    else:
        out.append((PASS, "paper_trading", "true"))
    enabled = [n.upper() for n in _MUST_BE_OFF if _s(settings, n, False) is True]
    if enabled:
        out.append((FAIL, "money_paths_off", "must be false for paper: " + ", ".join(enabled)))
    else:
        out.append((PASS, "money_paths_off", "live, customer-live, payout, TRON, sweep, Deriv, forex-live, arbitrage-live all off"))
    if _s(settings, "broker_sandbox") is not True:
        out.append((FAIL, "sandbox_when_paper", "BROKER_SANDBOX must be true while PAPER_TRADING is true (/readyz fails otherwise)"))
    else:
        out.append((PASS, "sandbox_when_paper", "BROKER_SANDBOX=true"))

    # 2. Readiness inputs (mirrors the /readyz checks that depend only on configuration).
    if encryption_check is not None:
        try:
            encryption_check()
            out.append((PASS, "encryption_configured", "encryption keyring is valid"))
        except Exception as exc:  # never echo the message: it can name key material
            out.append((FAIL, "encryption_configured", f"{type(exc).__name__}: set APP_ENCRYPTION_KEY (see scripts/generate_encryption_key.py)"))
    if not (_text(_s(settings, "secret_key")) or env == "development"):
        out.append((FAIL, "secret_configured", "SECRET_KEY is required outside development"))
    if non_dev:
        if not [x for x in _text(_s(settings, "admin_supabase_user_ids")).split(",") if x.strip()]:
            out.append((FAIL, "admin_auth", "ADMIN_SUPABASE_USER_IDS is empty; /readyz reports admin_auth=false"))
        else:
            out.append((PASS, "admin_auth", "admin user IDs configured (role lookup is checked at runtime)"))
    if non_dev:
        # Mirrors the /readyz release_identity check: SHA present, release_version == app_version, sha256 image digest.
        digest_ok = re.fullmatch(r"sha256:[0-9a-f]{64}", _text(_s(settings, "image_digest")).lower()) is not None
        version_ok = bool(_text(_s(settings, "release_version"))) and _text(_s(settings, "release_version")) == _text(_s(settings, "app_version"))
        if _text(_s(settings, "release_sha")) and version_ok and digest_ok:
            out.append((PASS, "release_identity", "RELEASE_SHA, RELEASE_VERSION and IMAGE_DIGEST are consistent"))
        else:
            missing = [n for n, ok in (("RELEASE_SHA", bool(_text(_s(settings, "release_sha")))),
                                       ("RELEASE_VERSION (must equal the app version)", version_ok),
                                       ("IMAGE_DIGEST (sha256:<64 hex>)", digest_ok)) if not ok]
            out.append((FAIL, "release_identity", "/readyz reports release_identity=false; fix: " + ", ".join(missing)))
    if _s(settings, "external_security_audit_required", True) and not _s(settings, "external_security_audit_enabled", True):
        out.append((FAIL, "external_security_audit", "required but disabled; /readyz reports false"))
    required = _s(settings, "external_custody_signer_required", True)
    signer_ok = (not required) or bool(_text(_s(settings, "external_custody_signer_url")) and _text(_s(settings, "external_custody_signer_hmac_secret")))
    if signer_ok:
        out.append((PASS, "custody_signer_configured", "satisfied"))
    else:
        out.append((WARN, "custody_signer_configured",
                    "not configured, so main-app /readyz reports false (HTTP 503 in staging/production while READYZ_FAIL_WHEN_NOT_READY is on). "
                    "Paper-only drills may set EXTERNAL_CUSTODY_SIGNER_REQUIRED=false while every money path is off"))
    if env == "production":
        if _s(settings, "distributed_rate_limit_required", False) and not _text(_s(settings, "redis_url")):
            out.append((FAIL, "distributed_rate_limiter", "REDIS_URL required in production"))
        if _s(settings, "require_backup_recovery_config", True) and not _text(_s(settings, "backup_recovery_url")):
            out.append((FAIL, "recovery_configured", "BACKUP_RECOVERY_URL required in production"))
    enforced = _s(settings, "readyz_fail_when_not_ready", True) is True
    if non_dev and not enforced:
        out.append((WARN, "readyz_enforcement", "READYZ_FAIL_WHEN_NOT_READY=false: /readyz returns 200 even when checks fail, so load balancers will not hold traffic"))
    if env == "development":
        out.append((INFO, "environment", "development: /readyz returns 200 even when not ready; staging/production return 503 unless READYZ_FAIL_WHEN_NOT_READY=false"))
    else:
        out.append((PASS, "environment", env or "unset"))

    # 3. Calibrated fill model: all-or-nothing, using the app's own rule.
    if calibration_check is not None:
        try:
            configured = calibration_check(settings)
        except Exception as exc:
            out.append((FAIL, "paper_fill_calibration", str(exc)[:300]))
        else:
            if configured is None:
                out.append((INFO, "paper_fill_calibration", "not configured: bar-driven fills, labelled INCOMPLETE_EXECUTION_MODEL"))
            else:
                evidence = _text(_s(settings, "paper_execution_evidence_path"))
                if evidence and not Path(evidence).exists():
                    out.append((WARN, "paper_fill_calibration", "complete, but the evidence path does not exist on this host (artifact hash is verified at runtime)"))
                else:
                    out.append((PASS, "paper_fill_calibration", "complete (artifact/dataset hashes are verified at runtime, not here)"))

    # 4. Customer paper ledger. The runtime paper API requires an explicit target URL;
    # SHARED_DATABASE_ENABLED only permits that target to be the primary database.
    if _s(settings, "customer_paper_ledger_enabled", False):
        if not _text(_s(settings, "customer_paper_database_url")):
            out.append((FAIL, "customer_paper_ledger", "enabled but CUSTOMER_PAPER_DATABASE_URL is empty; the shared-database flag does not replace the target URL"))
        else:
            out.append((PASS, "customer_paper_ledger", "enabled with an explicit database URL (role/RLS preflight runs at startup)"))
    else:
        out.append((INFO, "customer_paper_ledger", "disabled (default): customer paper orders are unavailable"))
    if _s(settings, "customer_totp_required", True):
        out.append((INFO, "customer_totp", "required: paper-test customers must have TOTP enrolled"))

    # 5. Limits a drill script can trip.
    out.append((INFO, "limits",
                f"{_s(settings, 'api_rate_limit_per_minute')}/min per IP "
                f"({_s(settings, 'expensive_api_rate_limit_per_minute')}/min for backtests and bot start), "
                f"auth {_s(settings, 'auth_rate_limit_per_minute')}/min, body cap {_s(settings, 'max_request_body_bytes')} bytes"))
    return out


def _safe_url(url: str) -> str:
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    # Query parameters may contain credentials too (for example password= or sslpassword=).
    # This string is display-only, so omit the entire query rather than risk exposing a token.
    return parsed.set(query={}).render_as_string(hide_password=True)


def _async_engine_url(url: str) -> str:
    """Normalize a plain PostgreSQL URL exactly as the customer-paper runtime does."""
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+asyncpg")
    return parsed.render_as_string(hide_password=False)


async def check_database(settings: Any) -> list[Result]:
    """Read-only database checks. Needs a reachable database; connection failures are FAILs."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    out: list[Result] = []
    url = _text(_s(settings, "database_url"))
    if not url:
        return [(FAIL, "database", "DATABASE_URL is not set")]
    if url.startswith("sqlite"):
        out.append((INFO, "migrations", "sqlite (development): Alembic head check skipped"))
    else:
        try:
            from alembic.config import Config
            from alembic.script import ScriptDirectory
            head = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
            engine = create_async_engine(url)
            try:
                async with engine.connect() as conn:
                    current = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
            finally:
                await engine.dispose()
            if current == head:
                out.append((PASS, "migrations", f"database is at head {head} ({_safe_url(url)})"))
            else:
                out.append((FAIL, "migrations", f"database is at {current!r}, code head is {head!r}; run alembic upgrade head"))
        except Exception as exc:
            out.append((FAIL, "migrations", f"cannot read migration state ({type(exc).__name__})"))

    # Mirrors /readyz execution_fencing: the single live_execution_lease row must exist.
    try:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                lease = (await conn.execute(text("SELECT 1 FROM live_execution_lease WHERE id = 1"))).scalar()
        finally:
            await engine.dispose()
        out.append((PASS, "execution_fencing", "live_execution_lease row present") if lease
                   else (FAIL, "execution_fencing", "live_execution_lease row id=1 is missing; /readyz reports execution_fencing=false (run alembic upgrade head)"))
    except Exception as exc:
        out.append((FAIL, "execution_fencing", f"cannot read live_execution_lease ({type(exc).__name__})"))

    if not _s(settings, "customer_paper_ledger_enabled", False):
        return out
    ledger_url = _text(_s(settings, "customer_paper_database_url")) or (url if _s(settings, "customer_paper_shared_database_enabled", False) else "")
    if not ledger_url or ledger_url.startswith("sqlite"):
        out.append((FAIL, "paper_ledger_db", "customer paper ledger needs a PostgreSQL target"))
        return out
    try:
        engine = create_async_engine(_async_engine_url(ledger_url))
        try:
            async with engine.connect() as conn:
                present = (await conn.execute(text("SELECT to_regclass('customer_paper.journals') IS NOT NULL"))).scalar()
                if not present:
                    out.append((FAIL, "paper_ledger_db", f"customer_paper schema not provisioned in {_safe_url(ledger_url)}"))
                    return out
                # Exact for the rows this role can see; row-level security may hide other tenants' rows,
                # so table statistics (approximate, visible to every role) are a second signal.
                visible = bool((await conn.execute(text("SELECT EXISTS (SELECT 1 FROM customer_paper.journals)"))).scalar())
                live = (await conn.execute(text(
                    "SELECT COALESCE(n_live_tup, 0) FROM pg_stat_all_tables "
                    "WHERE schemaname = 'customer_paper' AND relname = 'journals'"))).scalar() or 0
        finally:
            await engine.dispose()
    except Exception as exc:
        out.append((FAIL, "paper_ledger_db", f"cannot inspect ledger ({type(exc).__name__})"))
        return out
    if visible:
        out.append((WARN, "paper_ledger_fresh",
                    "journal rows already exist (exact check as this database role); repeat acceptance drills against "
                    "leftover rows fail (idempotency replays). Rebuild the ledger database before a clean drill"))
    elif int(live) > 0:
        out.append((WARN, "paper_ledger_fresh",
                    f"statistics estimate about {int(live)} journal rows that this role cannot see (row-level security) "
                    "or that were already deleted; treat as a warning, not proof, and confirm as the ledger owner role"))
    else:
        out.append((INFO, "paper_ledger_fresh",
                    "no journal rows visible to this role and none estimated; row-level security can hide rows, "
                    "so this is not proof the ledger is empty unless run as the ledger owner role"))
    return out


def _load_app_settings() -> tuple[Any | None, list[Result]]:
    try:
        import app.config as config
    except Exception as exc:
        # Pydantic/driver messages may include supplied secret values. Report only the type;
        # operators can inspect local logs without copying values into this preflight output.
        return None, [(FAIL, "settings_load", f"application configuration failed ({type(exc).__name__}); inspect local configuration without sharing secret values")]
    return config.settings, [(PASS, "settings_load", "configuration accepted by the app's own validators")]


def run(check_db: bool) -> list[Result]:
    settings, results = _load_app_settings()
    if settings is None:
        return results
    from app.crypto import validate_encryption_config
    from app.paper_execution_model import configured_paper_execution_settings
    results += check_settings(settings, encryption_check=validate_encryption_config,
                              calibration_check=configured_paper_execution_settings)
    if check_db:
        results += asyncio.run(check_database(settings))
        if _text(_s(settings, "redis_url")):
            from app.distributed import check_redis
            ok = asyncio.run(check_redis())
            results.append((PASS, "redis", "reachable") if ok else (FAIL, "redis", "REDIS_URL is set but the app's own ping check fails; /readyz reports distributed_rate_limiter=false"))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check-db", action="store_true", help="also run read-only database checks")
    parser.add_argument("--strict", action="store_true", help="treat WARN as failure (exit 2)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    results = run(args.check_db)
    fails = [r for r in results if r[0] == FAIL]
    warns = [r for r in results if r[0] == WARN]
    if args.json:
        print(json.dumps([{"status": s, "check": c, "detail": d} for s, c, d in results], indent=2))
    else:
        for status, check, detail in results:
            print(f"{status:<5} {check:<26} {detail}")
        print(f"\n{len(fails)} FAIL, {len(warns)} WARN, {sum(1 for r in results if r[0] == PASS)} PASS")
    if fails:
        return 1
    return 2 if (args.strict and warns) else 0


if __name__ == "__main__":
    raise SystemExit(main())
