from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_production_admin_totp_cannot_be_disabled():
    src = (ROOT / "app/config.py").read_text(encoding="utf-8")
    assert 'if self.environment == "production" and not self.admin_totp_required:' in src
    assert 'ADMIN_TOTP_REQUIRED must remain true in production' in src


def test_live_enable_requests_have_a_minimum_ttl():
    src = (ROOT / "app/config.py").read_text(encoding="utf-8")
    assert "live_enable_request_ttl_seconds: int = 300" in src
    assert 'LIVE_ENABLE_REQUEST_TTL_SECONDS must be >= 30' in src


def test_stale_live_enable_requests_are_rejected_and_cleared():
    src = (ROOT / "app/main.py").read_text(encoding="utf-8")
    block = src[src.index('@app.post("/api/live/enable")'):src.index('@app.post("/api/live/disable")')]
    assert "request_age = (now - requested_at).total_seconds()" in block
    assert "if request_age > settings.live_enable_request_ttl_seconds:" in block
    assert 'raise HTTPException(409, "Live-trading enablement request has expired; create a new request")' in block
    assert 's.live_enable_requested_at = None' in block
    assert 's.live_enabled = False' in block
    assert 's.mode = "PAPER"' in block
