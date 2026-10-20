from .config import settings
import json
import logging
import time
import uuid
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("ai_trader")
REQUESTS = Counter("trader_http_requests_total", "HTTP requests", ["method", "path", "status"])
REQUEST_LATENCY = Histogram("trader_http_request_duration_seconds", "HTTP request duration", ["method", "path"])
ORDERS = Counter("trader_orders_total", "Orders by mode/status", ["mode", "status"])
EQUITY = Gauge("trader_equity", "Current account equity")
DRAWDOWN = Gauge("trader_drawdown", "Current equity drawdown")
OPEN_POSITIONS = Gauge("trader_open_positions", "Number of non-zero positions")


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
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        path = request.url.path
        REQUESTS.labels(request.method, path, str(response.status_code)).inc()
        REQUEST_LATENCY.labels(request.method, path).observe(elapsed)
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
        logger.info(json.dumps({"request_id": request_id, "method": request.method, "path": path,
                                "status": response.status_code, "duration_ms": round(elapsed * 1000, 2)}))
        return response


def metrics_response():
    return generate_latest(), CONTENT_TYPE_LATEST
