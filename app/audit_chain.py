from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .db import AuditChainState, AuditLog, SessionLocal
from .distributed import allow_rate_limit


async def _lock_chain_state(db) -> AuditChainState:
    """Create/read and lock the single chain head before inspecting or appending rows."""
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


async def append_audit_once(
    db,
    *,
    event: str,
    detail: dict,
    actor_id: str = "",
    idempotency_fields: tuple[str, ...],
) -> tuple[AuditLog, bool]:
    """Append one audit event per stable detail identity while holding the chain lock."""
    if not idempotency_fields or any(field not in (detail or {}) for field in idempotency_fields):
        raise ValueError("audit idempotency fields must be present in the detail payload")

    await _lock_chain_state(db)
    existing_query = select(AuditLog).where(AuditLog.event == event)
    for field in idempotency_fields:
        value = json.dumps(detail[field], sort_keys=True, separators=(",", ":"), default=str)
        existing_query = existing_query.where(AuditLog.detail.contains(f'"{field}": {value}'))
    existing = (await db.execute(existing_query.order_by(AuditLog.id).limit(1))).scalar_one_or_none()
    if existing is not None:
        return existing, False

    row = await append_audit(db, event=event, detail=detail, actor_id=actor_id)
    return row, True


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
    # Lock before reading so PostgreSQL writers always hash from the committed predecessor.
    # populate_existing is required when this session already holds a stale identity-map object.
    state = await _lock_chain_state(db)

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


async def record_audit(event: str, detail: dict, actor_id: str = "system") -> AuditLog:
    """Append an audit row using a dedicated session (safe to call from any module)."""
    async with SessionLocal() as db:
        return await append_audit(db, event=event, detail=detail, actor_id=actor_id)
