from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_admin_dashboard_revalidates_mfa_with_server_before_protected_requests():
    script = (ROOT / "app/static/admin-dashboard.js").read_text(encoding="utf-8")
    template = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")

    assert "const adminTotpFlag=document.body.dataset.adminTotpRequired;" in script
    assert "const adminMfaRequired=adminTotpFlag!=='false';" in script
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


def test_admin_dashboard_discards_stale_requests_after_session_changes():
    script = (ROOT / "app/static/admin-dashboard.js").read_text(encoding="utf-8")

    assert "let adminSessionEpoch=0;" in script
    assert "const requestEpoch=adminSessionEpoch;" in script
    assert "const requestToken=token();" in script
    assert "requestEpoch===adminSessionEpoch&&requestToken===token()" in script
    assert "if(!sameSession){" in script
    assert "staleError.staleAdminSession=true;" in script
    assert "if(e?.staleAdminSession)return;" in script
    assert "Withdrawal queue: '+e.message" in script
    assert "Role management requires ADMINISTRATOR: '+esc(e.message)" in script
    assert "if(attemptEpoch!==adminSessionEpoch)return;" in script



def test_admin_dashboard_keeps_every_dispatched_handler_and_resumes_saved_session():
    script = (ROOT / "app/static/admin-dashboard.js").read_text(encoding="utf-8")

    for name in (
        "req",
        "loadWithdrawals",
        "approveWithdrawal",
        "executeWithdrawal",
        "prepareWithdrawalProposal",
        "reconcileWithdrawal",
        "rejectWithdrawal",
        "loadRoles",
        "saveRole",
        "loadMargin",
        "refresh",
        "run",
        "train",
        "backtest",
        "strategyBacktest",
        "backtestAll",
        "metaLabelAudit",
        "aiReview",
        "paperSignals",
        "signal",
        "strategySignal",
        "paperStep",
        "reconcile",
        "halt",
        "resetRisk",
        "disableLive",
    ):
        assert f"function {name}(" in script or f"async function {name}(" in script

    assert script.count("resumeAdminSession();") == 2  # login success + one page-load resume
    assert script.rstrip().endswith("resumeAdminSession();")

def test_admin_lock_clears_authenticator_secrets_and_preserves_sign_out_access():
    script = (ROOT / "app/static/admin-dashboard.js").read_text(encoding="utf-8")
    lock = script.split("function lockAdminDashboard(message,clearTokens=true){", 1)[1].split(
        "function activateDashboard(){", 1
    )[0]

    assert "adminSessionEpoch+=1;" in lock
    assert "window.adminMfaFactorId=null;" in lock
    assert "window.adminMfaChallengeId=null;" in lock
    assert "$('adminMfaEnrollment').hidden=true;" in lock
    assert "$('adminMfaQr').removeAttribute('src');" in lock
    assert "$('adminMfaSecret').textContent='';" in lock
    assert "$('adminMfaCode').value='';" in lock
    assert "$('adminLogoutBtn').hidden=!token();" in lock


def test_admin_login_and_mfa_verification_are_race_safe():
    script = (ROOT / "app/static/admin-dashboard.js").read_text(encoding="utf-8")
    login = script.split("async function adminLogin(){", 1)[1].split(
        "async function adminEnroll(){", 1
    )[0]
    verification = script.split("async function adminMfaVerify(){", 1)[1].split(
        "function adminLogout(){", 1
    )[0]

    parse_response = login.index("const result=await response.json().catch(()=>({}));")
    epoch_check = login.index("if(attemptEpoch!==adminSessionEpoch)return;", parse_response)
    token_install = login.index("sessionStorage.setItem('ait_admin_access_token',result.access_token);")
    assert parse_response < epoch_check < token_install
    assert "let adminMfaVerificationInFlight=false;" in script
    assert "if(adminMfaVerificationInFlight||dashboardReady)return;" in verification
    assert "let tokenUpgraded=false;" in verification
    assert "(!tokenUpgraded&&verificationToken!==token())" in verification
    assert "if(attemptEpoch===adminSessionEpoch)adminMfaVerificationInFlight=false;" in verification


def test_admin_mfa_retries_expired_challenges_and_recovers_partial_enrollment():
    script = (ROOT / "app/static/admin-dashboard.js").read_text(encoding="utf-8")
    enrollment = script.split("async function adminEnroll(){", 1)[1].split(
        "async function adminMfaVerify(){", 1
    )[0]
    verification = script.split("async function adminMfaVerify(){", 1)[1].split(
        "function adminLogout(){", 1
    )[0]
    enrollment_catch = enrollment.split("}catch(error){", 1)[1]

    assert "if(attemptEpoch!==adminSessionEpoch)return;" in enrollment_catch
    assert "$('adminMfaBox').style.display='block';" in enrollment_catch
    assert "$('adminMfaVerifyBtn').style.display='inline-block';" in enrollment_catch
    assert "window.adminMfaChallengeId=null;" in verification
    assert "if(!window.adminMfaChallengeId){" in verification
    assert "You can retry; a fresh challenge will be requested." in verification



def test_admin_dashboard_loaders_keep_numeric_formatting_and_single_dispatcher():
    script = (ROOT / "app/static/admin-dashboard.js").read_text(encoding="utf-8")

    expected_assignments = (
        "$('wdPendingAmount').textContent='$'+Number(summary.pending_amount||0).toLocaleString(undefined,{maximumFractionDigits:2});",
        "$('wdApprovedAmount').textContent=Number(summary.approved_amount||0).toLocaleString(undefined,{maximumFractionDigits:2})+' USDT reserved';",
        "$('marginRevenue').textContent='$'+Number(m.gross_subscription_revenue||0).toLocaleString(undefined,{maximumFractionDigits:2});",
        "$('marginCosts').textContent='$'+Number(m.operating_costs||0).toLocaleString(undefined,{maximumFractionDigits:2});",
        "$('marginRef').textContent='$'+Number(m.referral_commissions||0).toLocaleString(undefined,{maximumFractionDigits:2});",
        "$('marginPct').textContent=Number(m.contribution_margin_pct||0).toFixed(1)+'%';",
        "$('marginProfit').textContent='Contribution $'+Number(m.contribution_profit||0).toLocaleString(undefined,{maximumFractionDigits:2});",
        "$('equity').textContent='$'+Number(s.equity).toLocaleString(undefined,{maximumFractionDigits:2});",
        "$('dd').textContent=(s.drawdown*100).toFixed(2)+'%';",
        "$('daily').textContent='Daily loss '+(s.daily_loss*100).toFixed(2)+'%';",
    )
    for assignment in expected_assignments:
        assert assignment in script

    withdrawal_loader = script.split("async function loadWithdrawals(){", 1)[1].split(
        "async function approveWithdrawal(", 1
    )[0]
    margin_loader = script.split("async function loadMargin(){", 1)[1].split(
        "async function refresh(", 1
    )[0]
    refresh_loader = script.split("async function refresh(){", 1)[1].split(
        "async function run(", 1
    )[0]
    for loader in (withdrawal_loader, margin_loader, refresh_loader):
        assert "function adminLogout(" not in loader
        assert "document.addEventListener(" not in loader

    assert script.count("function adminLogout(){") == 1
    assert script.count("document.addEventListener('click'") == 1
    assert script.count("document.addEventListener('change'") == 1
    assert script.rstrip().endswith("resumeAdminSession();")
