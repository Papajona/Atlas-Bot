from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_admin_approver_token_is_not_persisted():
    source = (ROOT / "app/templates/dashboard.html").read_text()
    assert "sessionStorage.setItem('ait_approver_token'" not in source
    assert "X-Approver-Token" in source


def test_customer_session_status_is_not_hardcoded_as_authenticated():
    source = (ROOT / "app/templates/customer.html").read_text()
    assert "Supabase Auth · AAL2" not in source
    assert "id=" + '"sessionLabel"' in source
    assert "Session expired. Sign in again." in source


def test_production_csp_contains_required_browser_guards():
    source = (ROOT / "app/monitoring.py").read_text()
    assert "img-src 'self' data:" in source
    assert "object-src 'none'" in source
    assert "base-uri 'none'" in source
    assert "form-action 'self'" in source
    assert "frame-ancestors 'none'" in source


def test_risk_websocket_rechecks_role():
    source = (ROOT / "app/main.py").read_text()
    start = source.index("async def websocket_risk_telemetry")
    end = source.index('@app.get("/api/trades")', start)
    block = source[start:end]
    assert "last_auth_check = time.monotonic()" in block
    assert "time.monotonic() - last_auth_check > 30" in block
    assert 'await require_role(claims, "RISK_OFFICER")' in block
    assert "code=1008" in block


def test_android_allowlist_does_not_accept_arbitrary_cloud_run_hosts():
    source = (ROOT / "app/src/main/java/com/atlas/trading/MainActivity.kt").read_text()
    assert 'host.endsWith(".run.app")' not in source


def test_android_settings_do_not_claim_live_or_real_identity_without_backend_data():
    source = (ROOT / "app/src/main/java/com/atlas/trading/ui/ModernSettingsScreen.kt").read_text()
    for value in (
        "Alice Henderson",
        "alice.henderson@atlas.trading",
        "#AT-8824-LIVE",
        "VERIFIED PRO",
        "Connected • Cloud Run (14ms WebSocket)",
        "AUDIT_INITIALIZED",
    ):
        assert value not in source
