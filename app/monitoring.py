from .config import settings
import json
import logging
import re
import time
import uuid
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.routing import Match, Mount

logger = logging.getLogger("ai_trader")
REQUESTS = Counter("trader_http_requests_total", "HTTP requests", ["method", "path", "status"])
REQUEST_LATENCY = Histogram("trader_http_request_duration_seconds", "HTTP request duration", ["method", "path"])
ORDERS = Counter("trader_orders_total", "Orders by mode/status", ["mode", "status"])
EQUITY = Gauge("trader_equity", "Current account equity")
DRAWDOWN = Gauge("trader_drawdown", "Current equity drawdown")
OPEN_POSITIONS = Gauge("trader_open_positions", "Number of non-zero positions")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_KNOWN_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def safe_request_id(candidate: str | None) -> str:
    value = (candidate or "").strip()
    return value if _REQUEST_ID_RE.fullmatch(value) else uuid.uuid4().hex


def metric_path_label(request: Request) -> str:
    """Label routes and mounted applications without using attacker-controlled paths."""
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template

    # Starlette Mount matches before setting scope["route"], unlike APIRoute.
    # Recover the server-defined mount path so static-file requests do not get
    # mixed into the "unmatched" bucket used for real 404/scanner traffic.
    # After dispatch, root_path may include the consumed mount prefix. Restore
    # only the original application prefix (for example, /gateway) so matching
    # retains proxy/parent prefixes while exposing the mounted path (/static).
    # Match on a copy; never mutate the live ASGI request scope.
    match_scope = dict(request.scope)
    app_root_path = request.scope.get("app_root_path")
    match_scope["root_path"] = (
        app_root_path.rstrip("/")
        if isinstance(app_root_path, str) and app_root_path.rstrip("/")
        else ""
    )
    mount_paths = []
    for candidate in getattr(request.app, "routes", ()):
        if not isinstance(candidate, Mount):
            continue
        try:
            match, _child_scope = candidate.matches(match_scope)
        except (KeyError, TypeError, ValueError):
            continue
        mount_path = getattr(candidate, "path", None)
        if match is Match.FULL and isinstance(mount_path, str) and mount_path:
            mount_paths.append(mount_path)
    return max(mount_paths, key=len) if mount_paths else "unmatched"



# The admin dashboard now uses a same-origin external script and delegated data-action handlers.
# Keep inline scripts blocked on /admin. The customer dashboard still contains inline handlers
# and therefore retains a compatibility policy until that separate page is migrated.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
COMPAT_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


class TelemetryMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = safe_request_id(request.headers.get("x-request-id"))
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        path = request.url.path
        method_label = request.method if request.method in _KNOWN_METHODS else "OTHER"
        path_label = metric_path_label(request)
        REQUESTS.labels(method_label, path_label, str(response.status_code)).inc()
        REQUEST_LATENCY.labels(method_label, path_label).observe(elapsed)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if str(settings.environment).lower() == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if str(settings.environment).lower() in {"staging", "production"}:
            # The customer portal (/) and admin console use external same-origin scripts.
            # FastAPI's interactive API docs include inline bootstrap code, so retain a
            # separate compatibility policy only for those documentation routes.
            if path == "/" or path in {"/admin", "/admin/"}:
                policy = CONTENT_SECURITY_POLICY
            elif path == "/docs" or path.startswith("/docs/") or path == "/redoc" or path.startswith("/redoc/"):
                policy = COMPAT_CONTENT_SECURITY_POLICY
            else:
                policy = CONTENT_SECURITY_POLICY
            response.headers["Content-Security-Policy"] = policy
        logger.info(json.dumps({"request_id": request_id, "method": request.method, "path": path[:256],
                                "status": response.status_code, "duration_ms": round(elapsed * 1000, 2)}))
        return response


def metrics_response():
    return generate_latest(), CONTENT_TYPE_LATEST
