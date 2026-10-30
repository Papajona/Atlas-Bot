from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

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


async def _rate_limited_detail(detail: dict, actor_id: str) -> dict:
    """Perform the Redis round trip before taking the global audit-chain lock."""
    allowed, _remaining = await allow_rate_limit(
        f"audit_chain:{actor_id or 'system'}", limit=100, window_seconds=60
    )
    audit_detail = dict(detail or {})
    if not allowed:
        audit_detail["audit_chain_rate_limited"] = True
        audit_detail["audit_chain_rate_limit"] = {"limit": 100, "window_seconds": 60}
    return audit_detail


async def _append_audit_locked(
    db,
    *,
    event: str,
    audit_detail: dict,
    actor_id: str,
    state: AuditChainState,
    idempotency_key: str | None = None,
) -> AuditLog:
    """Append using an already-locked chain head; no network calls occur under the lock."""
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
        idempotency_key=idempotency_key,
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
    idempotency_fields: tuple[str, ...],
) -> tuple[AuditLog | SimpleNamespace, bool]:
    """Append once by a hashed identity, without scanning or decrypting the audit log."""
    if not idempotency_fields or any(field not in (detail or {}) for field in idempotency_fields):
        raise ValueError("audit idempotency fields must be present in the detail payload")

    audit_detail = await _rate_limited_detail(detail, actor_id)
    identity = {field: detail[field] for field in idempotency_fields}
    identity_payload = json.dumps(
        {"event": event, "identity": identity},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    idempotency_key = hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()

    # The unique (event, idempotency_key) index makes this a bounded indexed lookup.
    # Select only non-encrypted columns: a damaged historical detail payload must not
    # prevent a retry from recognizing its already-persisted submission event.
    state = await _lock_chain_state(db)
    existing = (
        await db.execute(
            select(AuditLog.id, AuditLog.event_hash)
            .where(
                AuditLog.event == event,
                AuditLog.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
    ).first()
    if existing is not None:
        return SimpleNamespace(id=existing.id, event_hash=existing.event_hash), False

    row = await _append_audit_locked(
        db,
        event=event,
        audit_detail=audit_detail,
        actor_id=actor_id,
        state=state,
        idempotency_key=idempotency_key,
    )
    return row, True


async def append_audit(db, *, event: str, detail: dict, actor_id: str = "") -> AuditLog:
    # Preserve the established rate-limit annotation, but complete its Redis round
    # trip before locking the chain head so all other audit writers are not blocked.
    audit_detail = await _rate_limited_detail(detail, actor_id)
    state = await _lock_chain_state(db)
    return await _append_audit_locked(
        db,
        event=event,
        audit_detail=audit_detail,
        actor_id=actor_id,
        state=state,
    )


async def record_audit(event: str, detail: dict, actor_id: str = "system") -> AuditLog:
    """Append an audit row using a dedicated session (safe to call from any module)."""
    async with SessionLocal() as db:
        return await append_audit(db, event=event, detail=detail, actor_id=actor_id)
