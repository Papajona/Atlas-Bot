"""Consent-gated encryption and retention for strategy benchmark submissions."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, text

from .config import settings
from .crypto import EncryptionKeyConfigurationError, encrypt_text
from .db import BenchmarkPromptCapture

logger = logging.getLogger(__name__)

CONSENT_VERSION = "atlas-strategy-benchmark-v1"
RETENTION_DAYS = 90
MAX_CAPTURES_PER_24_HOURS = 20

_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_SECRET_RE = re.compile(
    r"(?i)\b(?:password|passphrase|seed phrase|mnemonic|private key|api[_ -]?key|access token)\b"
)




async def benchmark_capture_permissions(db) -> dict[str, bool]:
    """Inspect effective permissions without touching benchmark data rows."""
    get_bind = getattr(db, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    dialect = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect != "postgresql":
        return {"can_select": True, "can_insert": True, "can_delete": True}
    row = (await db.execute(text("""
        SELECT
          CASE WHEN to_regclass('public.customer_benchmark_captures') IS NULL
               THEN false ELSE has_table_privilege(
                   current_user, to_regclass('public.customer_benchmark_captures'), 'SELECT'
               ) END AS can_select,
          CASE WHEN to_regclass('public.customer_benchmark_captures') IS NULL
               THEN false ELSE has_table_privilege(
                   current_user, to_regclass('public.customer_benchmark_captures'), 'INSERT'
               ) END AS can_insert,
          CASE WHEN to_regclass('public.customer_benchmark_captures') IS NULL
               THEN false ELSE has_table_privilege(
                   current_user, to_regclass('public.customer_benchmark_captures'), 'DELETE'
               ) END AS can_delete
    """))).mappings().one()
    return {
        "can_select": bool(row["can_select"]),
        "can_insert": bool(row["can_insert"]),
        "can_delete": bool(row["can_delete"]),
    }


async def purge_expired_benchmark_captures(db, *, now: datetime | None = None) -> int:
    """Physically delete expired captures; pg_cron is supplemented by an app worker."""
    cutoff = now or datetime.now(timezone.utc)
    result = await db.execute(
        delete(BenchmarkPromptCapture).where(BenchmarkPromptCapture.expires_at <= cutoff)
    )
    return max(0, int(result.rowcount or 0))

def contains_obvious_sensitive_text(prompt: str) -> bool:
    """Conservative guard for obvious contact/credential strings; not a PII detector."""
    return bool(_EMAIL_RE.search(prompt) or _SECRET_RE.search(prompt))


async def capture_strategy_prompt(
    db,
    *,
    customer_id: int,
    prompt: str,
    native_output: dict[str, Any],
    asset: str,
    symbol: str,
    exchange: str,
    timeframe: str,
) -> dict[str, Any]:
    """Persist a prompt/output pair only after explicit, per-request user consent."""
    if not prompt or not isinstance(prompt, str):
        return {"status": "SKIPPED_INVALID_PROMPT"}
    if contains_obvious_sensitive_text(prompt):
        return {"status": "SKIPPED_POSSIBLE_SENSITIVE_CONTENT"}
    try:
        # The active application Fernet key is required; never store plaintext as fallback.
        # Prefix the caller-controlled plaintext with a fixed domain separator:
        # encrypt_text is idempotent for values already beginning "enc:", so
        # never pass an untrusted prompt directly to it.
        encrypted_prompt = encrypt_text("atlas-benchmark-prompt-v1:" + prompt)
        encrypted_output = encrypt_text(
            "atlas-benchmark-output-v1:" + json.dumps(
                native_output, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            )
        )
    except (EncryptionKeyConfigurationError, RuntimeError, ValueError, TypeError):
        logger.warning("benchmark_capture_not_stored reason=encryption_unavailable")
        return {"status": "SKIPPED_ENCRYPTION_UNAVAILABLE"}

    now = datetime.now(timezone.utc)
    try:
        # Permission, lock, count, and insert queries must all run in savepoints.
        # A failed optional capture must not leave the caller's candidate/credit
        # transaction aborted when strategy creation continues.
        async with db.begin_nested():
            permissions = await benchmark_capture_permissions(db)
        if not permissions["can_select"] or not permissions["can_insert"]:
            return {"status": "SKIPPED_STORAGE_UNAVAILABLE"}

        async with db.begin_nested():
            # Serialize concurrent submissions per customer so the daily cap cannot
            # be bypassed by two requests that both observe the same previous count.
            if db.get_bind().dialect.name == "postgresql":
                await db.execute(
                    text("SELECT pg_advisory_xact_lock(17012026, :customer_id)"),
                    {"customer_id": customer_id},
                )
            recent = int((await db.execute(
                select(func.count(BenchmarkPromptCapture.id)).where(
                    BenchmarkPromptCapture.customer_id == customer_id,
                    BenchmarkPromptCapture.created_at >= now - timedelta(hours=24),
                    BenchmarkPromptCapture.expires_at > now,
                )
            )).scalar_one() or 0)
            if recent >= MAX_CAPTURES_PER_24_HOURS:
                return {"status": "SKIPPED_DAILY_LIMIT"}
            row = BenchmarkPromptCapture(
                customer_id=customer_id,
                prompt_ciphertext=encrypted_prompt,
                native_output_ciphertext=encrypted_output,
                consent_version=CONSENT_VERSION,
                consented_at=now,
                asset=asset,
                symbol=symbol,
                exchange=exchange,
                timeframe=timeframe,
                native_ai_version=str(settings.app_version or "")[:80],
                created_at=now,
                expires_at=now + timedelta(days=RETENTION_DAYS),
            )
            db.add(row)
            await db.flush()
    except Exception:
        logger.exception("benchmark_capture_storage_failed customer_id=%s", customer_id)
        return {"status": "SKIPPED_STORAGE_ERROR"}
    return {"status": "STORED", "retention_days": RETENTION_DAYS, "consent_version": CONSENT_VERSION}
