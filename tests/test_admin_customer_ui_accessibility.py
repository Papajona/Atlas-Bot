from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_admin_dashboard_does_not_fetch_or_poll_protected_data_before_authentication():
    dashboard = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    assert "let dashboardReady = false;" in dashboard
    assert "if (!dashboardReady && !path.startsWith('/api/admin/auth/'))" in dashboard
    assert "if (!dashboardReady) return;" in dashboard
    assert "resumeAdminSession();" in dashboard
    assert "setInterval(() => {" in dashboard
    assert "if (dashboardReady) refresh();" in dashboard
    assert "if (dashboardReady) loadWithdrawals();" in dashboard
    assert "refresh();loadWithdrawals();loadRoles();loadMargin();setInterval(refresh,5000)" not in dashboard


def test_admin_dashboard_requires_verified_aal2_before_unlocking_production():
    dashboard = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    assert 'const adminMfaRequired = {{ "true" if settings.admin_totp_required else "false" }};' in dashboard
    assert "status.enabled && status.aal === 'aal2'" in dashboard
    assert "activateDashboard();" in dashboard
    assert "lockAdminDashboard('Administrator session expired or was rejected. Sign in again.')" in dashboard
    assert "Authenticator response received, but the server did not confirm AAL2. Protected data remains locked." in dashboard
    assert 'id="adminMfaEnrollment"' in dashboard
    assert 'id="adminMfaQr" alt="QR code for enrolling the administrator authenticator"' in dashboard


def test_admin_auth_controls_and_dynamic_results_are_accessible():
    dashboard = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    css = (ROOT / "app/static/app.css").read_text(encoding="utf-8")
    assert '<label for="adminEmail">Admin email</label>' in dashboard
    assert '<label for="adminPassword">Admin password</label>' in dashboard
    assert '<label for="adminMfaCode">Authenticator code</label>' in dashboard
    assert 'id="authState" class="muted" role="status" aria-live="polite" aria-atomic="true"' in dashboard
    assert 'id="output" role="region" aria-label="Admin action results" aria-live="polite"' in dashboard
    for table_body in ('withdrawals', 'roles', 'trades'):
        assert f'<tbody id="{table_body}" aria-live="polite"' in dashboard
    assert 'aria-label="Open admin navigation"' in dashboard
    assert 'aria-label="Administrator profile"' in dashboard
    assert ":focus-visible" in css
    assert "prefers-reduced-motion:reduce" in css
    assert "body:not(.admin-session-ready) main>section:not(#overview){display:none}" in css


def test_customer_feedback_is_announced_and_referral_input_has_a_programmatic_label():
    customer = (ROOT / "app/templates/customer.html").read_text(encoding="utf-8")
    status_ids = ("authOut", "mfaOut", "smartOut", "botOut", "dcaOut", "strategyOut",
                  "labOut", "executorOut", "otpOut", "wdOut", "billingOut")
    for status_id in status_ids:
        assert f'<pre id="{status_id}" class="feedback" role="status" aria-live="polite" aria-atomic="true">' in customer
    assert '<label class="referral-label" for="referralCode">Referral code</label>' in customer
    assert 'id="referralCode" aria-describedby="billingOut"' in customer
