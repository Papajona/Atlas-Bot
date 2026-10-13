from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_admin_login_does_not_claim_unverified_hardware_key_or_biometric_protection():
    source = (ROOT / "app/src/main/java/com/atlas/trading/AdminLoginScreen.kt").read_text(encoding="utf-8")
    assert "Server-verified admin session required" in source
    assert "Hardware Key & Biometric Safe" not in source
    assert "Encrypted TLS 1.3" not in source


def test_mfa_settings_do_not_claim_every_users_factor_is_already_verified():
    source = (ROOT / "app/src/main/java/com/atlas/trading/ui/ModernSettingsScreen.kt").read_text(encoding="utf-8")
    assert 'badge = "AAL2 REQUIRED"' in source
    assert "Server-verified TOTP required for protected actions" in source
    assert "Status: Active & Verified" not in source
    assert "This page cannot confirm that an authenticator is enrolled or currently verified." in source


def test_risk_setting_does_not_attribute_stale_percentage_to_audit():
    source = (ROOT / "app/src/main/java/com/atlas/trading/ui/ModernSettingsScreen.kt").read_text(encoding="utf-8")
    assert "Audit recommends 0.5%" not in source
    assert "strategy and server risk caps may enforce a lower limit" in source
