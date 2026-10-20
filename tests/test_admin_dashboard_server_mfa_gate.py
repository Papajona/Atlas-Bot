from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_admin_dashboard_revalidates_mfa_with_server_before_protected_requests():
    script = (ROOT / "app/static/admin-dashboard.js").read_text(encoding="utf-8")
    template = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")

    assert "const adminMfaRequired=document.body.dataset.adminTotpRequired==='true';" in script
    assert "if(!dashboardReady&&!String(path).startsWith('/api/admin/auth/'))" in script
    assert "const status=await api('/api/admin/auth/mfa/status');" in script
    assert "status.enabled&&status.aal==='aal2'" in script
    assert "function resumeAdminSession()" in script
    assert "resumeAdminSession();" in script
    assert "if(dashboardReady)refresh()" in script
    assert "if(dashboardReady)loadWithdrawals()" in script
    assert "data-admin-totp-required=" in template


def test_admin_sensitive_sections_are_hidden_before_server_verified_session():
    css = (ROOT / "app/static/app.css").read_text(encoding="utf-8")
    assert "body:not(.admin-session-ready) main>section:not(#overview){display:none}" in css
    assert "function lockAdminDashboard(message,clearTokens=true)" in (
        ROOT / "app/static/admin-dashboard.js"
    ).read_text(encoding="utf-8")


def test_admin_mfa_enrollment_and_status_announcements_are_accessible():
    template = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    assert '<label for="adminEmail">Admin email</label>' in template
    assert '<label for="adminPassword">Admin password</label>' in template
    assert 'id="adminMfaCode" inputmode="numeric" autocomplete="one-time-code" maxlength="6" pattern="[0-9]{6}" aria-describedby="authState"' in template
    assert 'id="authState" class="muted" role="status" aria-live="polite" aria-atomic="true"' in template
    assert 'id="adminMfaQr" alt="QR code for enrolling the administrator authenticator"' in template
    assert 'id="output" role="region" aria-label="Admin action results" aria-live="polite"' in template
    assert 'id="withdrawalRows" aria-live="polite"' in template
    assert 'id="roles" aria-live="polite"' in template
    assert 'id="trades" aria-live="polite"' in template
