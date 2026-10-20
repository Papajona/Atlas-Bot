from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_production_admin_mfa_cannot_be_disabled():
    source = (ROOT / "app/config.py").read_text(encoding="utf-8")
    assert 'if self.environment == "production" and not self.admin_totp_required:' in source
    assert "ADMIN_TOTP_REQUIRED must remain true in production" in source


def test_live_enable_approval_expiry_is_positive_and_reused_by_current_gate():
    source = (ROOT / "app/config.py").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "live_enable_request_ttl_minutes: int = 15" in source
    assert 'LIVE_ENABLE_REQUEST_TTL_MINUTES must be >= 1' in source
    gate = main[main.index("def _live_request_expired"):main.index('@app.post("/api/live/enable")')]
    assert "settings.live_enable_request_ttl_minutes" in gate
    block = main[main.index('@app.post("/api/live/enable")'):main.index('@app.post("/api/live/disable")')]
    assert "_live_request_expired(s.live_enable_requested_at, now)" in block
    assert "expired during broker preflight" in block
