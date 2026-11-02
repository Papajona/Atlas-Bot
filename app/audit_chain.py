from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .db import AuditChainState, AuditLog, SessionLocal
from .distributed import allow_rate_limit


async def _lock_audit_chain_state(db) -> AuditChainState:
    """Serialize audit writers and return the locked chain head."""
    locked = (
        select(AuditChainState)
        .where(AuditChainState.id == 1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    state = (await db.execute(locked)).scalar_one_or_none()
    if state is None:
        try:
            async with db.begin_nested():
                db.add(AuditChainState(id=1, last_hash=""))
                await db.flush()
        except IntegrityError:
            pass
        state = (await db.execute(locked)).scalar_one()
    return state


async def append_audit(db, *, event: str, detail: dict, actor_id: str = "") -> AuditLog:
    # Bound abusive event volume per actor using the shared Redis limiter when configured.
    # Never drop security/accounting events: annotate limited events and still append them.
    allowed, remaining = await allow_rate_limit(
        f"audit_chain:{actor_id or 'system'}", limit=100, window_seconds=60
    )
    audit_detail = dict(detail or {})
    if not allowed:
        audit_detail["audit_chain_rate_limited"] = True
        audit_detail["audit_chain_rate_limit"] = {"limit": 100, "window_seconds": 60}
    state = await _lock_audit_chain_state(db)

    created = datetime.now(timezone.utc)
    previous = state.last_hash or ""
    payload = json.dumps(
        {
            "event": event,
            "detail": audit_detail,
            "actor_id": actor_id,
            "created_at": created.isoformat(),
            "previous_hash": previous,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    event_hash = hashlib.sha256(payload.encode()).hexdigest()
    row = AuditLog(
        event=event,
        detail=json.dumps(audit_detail, default=str),
        actor_id=actor_id,
        previous_hash=previous,
        event_hash=event_hash,
        created_at=created,
    )
    db.add(row)
    state.last_hash = event_hash
    state.updated_at = created
    await db.commit()
    return row


async def append_audit_once(
    db,
    *,
    event: str,
    detail: dict,
    actor_id: str = "",
    idempotency_key: str,
) -> tuple[AuditLog, bool]:
    """Append a durable audit event once per stable key, including after failed deliveries.

    The chain-state row lock serializes lookups and appends. Existing encrypted detail is
    decrypted by the AuditLog ORM type before its idempotency key is compared.
    """
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 200:
        raise ValueError("audit idempotency key must be non-empty and at most 200 characters")
    await _lock_audit_chain_state(db)
    prior_rows = (await db.execute(
        select(AuditLog).where(AuditLog.event == event).order_by(AuditLog.id.desc())
    )).scalars().all()
    for prior in prior_rows:
        try:
            prior_detail = json.loads(prior.detail or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(prior_detail, dict) and prior_detail.get("_audit_idempotency_key") == key:
            return prior, False
    safe_detail = dict(detail or {})
    safe_detail["_audit_idempotency_key"] = key
    row = await append_audit(db, event=event, detail=safe_detail, actor_id=actor_id)
    return row, True


async def record_audit(event: str, detail: dict, actor_id: str = "system") -> AuditLog:
    """Append an audit row using a dedicated session (safe to call from any module)."""
    async with SessionLocal() as db:
        return await append_audit(db, event=event, detail=detail, actor_id=actor_id)
