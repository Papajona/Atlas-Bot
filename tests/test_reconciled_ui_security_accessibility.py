from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_customer_referral_label_and_feedback_regions_are_accessible():
    source = (ROOT / "app/templates/customer.html").read_text(encoding="utf-8")
    assert '<label class="referral-label" for="referralCode">Referral code</label>' in source
    assert 'id="referralCode" aria-describedby="billingOut"' in source
    for output_id in ("authOut", "mfaOut", "otpOut", "wdOut", "billingOut"):
        assert (
            f'id="{output_id}" class="feedback" role="status" '
            'aria-live="polite" aria-atomic="true"'
        ) in source


def test_both_content_security_policies_deny_base_tag_injection():
    source = (ROOT / "app/monitoring.py").read_text(encoding="utf-8")
    assert source.count("base-uri 'none'") == 2


def test_mobile_settings_do_not_claim_unverified_mfa_biometrics_telemetry_or_audit():
    source = (ROOT / "app/src/main/java/com/atlas/trading/ui/ModernSettingsScreen.kt").read_text(encoding="utf-8")
    for claim in (
        "ACTIVE (AAL2)",
        "Google Authenticator TOTP enforced",
        "Require Fingerprint / Device Credential to unlock app",
        "AES-GCM",
        "Connected • Cloud Run (14ms WebSocket)",
        "AUDIT_INITIALIZED",
        "SYSTEM_INTEGRITY_PASSED",
    ):
        assert claim not in source, f"unverified mobile security claim remains: {claim}"
    assert "NOT VERIFIED HERE" in source
    assert "Not connected • live telemetry unavailable" in source
    assert "No live audit events are available in this build." in source


def test_admin_role_revocation_and_websocket_reauthorization_remain_fail_closed():
    rbac = (ROOT / "app/admin_rbac.py").read_text(encoding="utf-8")
    assert "select(AdminRole.role, AdminRole.active)" in rbac
    assert "including an inactive one, makes the database authoritative" in rbac
    assert "if rows:" in rbac

    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    start = source.index("async def websocket_risk_telemetry")
    end = source.index('@app.get("/api/trades")', start)
    block = source[start:end]
    assert "last_auth_check = time.monotonic()" in block
    assert "time.monotonic() - last_auth_check >= 30.0" in block
    assert 'await require_role(claims, "RISK_OFFICER")' in block
    assert "code=1008" in block
