from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from sqlalchemy import select
from .config import settings
from .db import Incident, SessionLocal

_SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
_log = logging.getLogger(__name__)

async def _send_alert(key: str, severity: str, category: str, summary: str) -> bool:
    url = str(settings.alert_webhook_url or "").strip()
    if not url or _SEVERITY_RANK.get(str(severity).upper(), 0) < _SEVERITY_RANK.get(str(settings.alert_min_severity).upper(), 3):
        return False
    try:
        import httpx
        payload = {"text": f"[Atlas {str(severity).upper()}] {category}: {summary[:300]} (key={key[:120]})"}
        async with httpx.AsyncClient(timeout=float(settings.alert_webhook_timeout_seconds), follow_redirects=False) as client:
            response = await client.post(url, json=payload)
        if response.status_code >= 300:
            _log.warning("incident_alert_rejected status=%s", response.status_code)
            return False
        return True
    except Exception:
        _log.warning("incident_alert_failed", exc_info=True)
        return False

async def open_incident(*, key: str, severity: str, category: str, summary: str, detail: dict, customer_id: int | None = None) -> int | None:
    async with SessionLocal() as db:
        row = (await db.execute(select(Incident).where(Incident.incident_key == key).with_for_update())).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        notify = row is None
        if row:
            if row.status == "RESOLVED":
                notify = True
                row.status = "OPEN"
                row.resolved_at = None
                row.resolved_by = ""
            if _SEVERITY_RANK.get(str(severity).upper(), 0) > _SEVERITY_RANK.get(str(row.severity or "").upper(), 0):
                notify = True
            row.severity = severity
            row.category = category
            row.summary = summary[:500]
            row.detail_json = json.dumps(detail, default=str)
            row.updated_at = now
        else:
            row = Incident(incident_key=key, severity=severity, status="OPEN", category=category, customer_id=customer_id, summary=summary[:500], detail_json=json.dumps(detail, default=str), opened_at=now, updated_at=now)
            db.add(row)
        await db.commit()
        incident_id = row.id
    if notify:
        await _send_alert(key, severity, category, summary)
    return incident_id
async def resolve_incident(key: str, actor_id: str = "") -> bool:
    async with SessionLocal() as db:
        row = (await db.execute(select(Incident).where(Incident.incident_key == key).with_for_update())).scalar_one_or_none()
        if not row:
            return False
        row.status = "RESOLVED"; row.resolved_at = datetime.now(timezone.utc); row.resolved_by = actor_id[:160]
        await db.commit()
        return True
