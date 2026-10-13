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
    assert "Not connected to saved server preferences yet." in source
    assert "server risk caps remain authoritative" in source


def test_execution_sensitive_settings_are_not_fake_local_controls():
    source = (ROOT / "app/src/main/java/com/atlas/trading/ui/ModernSettingsScreen.kt").read_text(encoding="utf-8")
    assert 'title = "Execution mode"' in source
    assert "Server-controlled. This screen cannot enable live trading" in source
    assert "var isPaperTrading" not in source
    assert "Live order routing active" not in source

    risk_start = source.index('title = "Risk Budget Per Trade"')
    risk_end = source.index("HorizontalDivider", risk_start)
    assert "enabled = false" in source[risk_start:risk_end]

    stop_start = source.index('title = "Automated Trailing Stop-Loss"')
    stop_end = source.index("HorizontalDivider", stop_start)
    assert "enabled = false" in source[stop_start:stop_end]

    cost_start = source.index('title = "Cost-Stress Gate (2.0x Multiplier)"')
    cost_end = source.index("HorizontalDivider", cost_start)
    assert "enabled = false" in source[cost_start:cost_end]

    slip_start = source.index('title = "Max Slippage Tolerance"')
    slip_end = source.index("            }\n        }", slip_start)
    assert "enabled = false" in source[slip_start:slip_end]
