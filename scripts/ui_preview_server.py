"""Isolated rendering harness for the real Atlas HTML templates; never talks to a real backend."""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))

# Explicitly synthetic, safe values used only to render the admin template in CI.
PREVIEW_SETTINGS = {
    "max_drawdown": 0.15,
    "daily_loss_limit": 0.03,
    "max_leverage": 3.0,
    "max_total_exposure_usd": 20000.0,
    "paper_trading": True,
    "broker_sandbox": True,
}


@app.get("/_preview/healthz")
async def preview_health():
    return {"ok": True, "mode": "isolated-ui-preview", "backend_calls": "stubbed"}


@app.get("/")
async def customer_portal(request: Request):
    return templates.TemplateResponse(request, "customer.html", {"settings": PREVIEW_SETTINGS})


@app.get("/admin")
async def admin_dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {"settings": PREVIEW_SETTINGS})


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def stub_api(path: str):
    # Keep the rendered page deterministic and guarantee no API call reaches a live service.
    # Return each UI endpoint's documented top-level shape so preview screenshots represent
    # normal empty/loading states rather than artificial "rows.map is not a function" errors.
    if path == "state":
        return JSONResponse({
            "mode": "PAPER", "equity": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0,
            "drawdown": 0.0, "daily_loss": 0.0, "live_enabled": False,
            "kill_switch": False, "open_positions": [],
        })
    if path == "trades" or path == "admin/withdrawals" or path == "admin/security/roles":
        return JSONResponse([])
    if path == "admin/withdrawals/summary":
        return JSONResponse({
            "pending_count": 0, "pending_amount": 0.0,
            "approved_count": 0, "approved_amount": 0.0,
            "in_flight_count": 0, "rejected_count": 0,
        })
    if path == "admin/billing/margin":
        return JSONResponse({
            "gross_subscription_revenue": 0.0, "operating_costs": 0.0,
            "referral_commissions": 0.0, "contribution_margin_pct": 0.0,
            "contribution_profit": 0.0,
        })
    if path == "customer/me":
        return JSONResponse({"email": "preview@example.invalid", "status": "PREVIEW"})
    if path in {"customer/wallets", "customer/funding", "customer/withdrawals"}:
        return JSONResponse([])
    return JSONResponse({"ok": True, "preview": True, "status": "PAPER", "items": [], "data": []})


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
