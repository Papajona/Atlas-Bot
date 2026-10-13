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
    return JSONResponse({"ok": True, "preview": True, "status": "PAPER", "items": [], "data": []})


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
