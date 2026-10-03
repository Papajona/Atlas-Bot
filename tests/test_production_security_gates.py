from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_android_release_never_uses_debug_keystore():
    src = (ROOT / "app" / "build.gradle.kts").read_text()
    release = src[src.index('release {'):]
    assert 'signingConfig = signingConfigs.getByName("debugConfig")' not in release
    assert "ATLAS_RELEASE_STORE_PASSWORD" in src
    assert "ATLAS_RELEASE_KEY_ALIAS" in src
    assert "ATLAS_RELEASE_KEY_PASSWORD" in src


def test_risk_telemetry_is_not_public():
    src = (ROOT / "app" / "main.py").read_text()
    start = src.index('@app.get("/api/risk-dashboard/metrics")')
    end = src.index('@app.websocket("/ws/risk-telemetry")', start)
    block = src[start:end]
    assert "authorization: str | None = Header(default=None)" in block
    assert "claims = await auth(x_admin_token, authorization)" in block
    assert 'await require_role(claims, "RISK_OFFICER")' in block


def test_risk_websocket_authenticates_before_accept():
    src = (ROOT / "app" / "main.py").read_text()
    start = src.index('@app.websocket("/ws/risk-telemetry")')
    block = src[start:src.index('@app.get("/api/trades")', start)]
    assert "authorization: str | None = Header(default=None)" in block
    assert 'authorization = authorization or websocket.headers.get("authorization")' in block
    assert "await auth(None, authorization)" in block
    assert 'await websocket.close(code=1008' in block
    assert "await websocket.accept()" in block


def test_android_risk_controls_require_authenticated_server_session():
    auth = (ROOT / "app" / "src" / "main" / "java" / "com" / "atlas" / "trading" / "auth" / "AdminAuthClient.kt").read_text()
    main = (ROOT / "app" / "src" / "main" / "java" / "com" / "atlas" / "trading" / "MainActivity.kt").read_text()
    risk = (ROOT / "app" / "src" / "main" / "java" / "com" / "atlas" / "trading" / "ui" / "RiskDashboard.kt").read_text()
    assert "/api/admin/auth/login" in auth
    assert "/api/admin/auth/mfa/challenge" in auth
    assert "/api/admin/auth/mfa/verify" in auth
    assert "/api/risk/kill" in auth
    assert "/api/risk/reset" in auth
    assert 'Authorization", "Bearer $bearer"' in auth
    assert "setKillSwitch(isHalted, token)" in main
    assert "accessToken: String? = null" in risk
    assert 'conn.setRequestProperty("Authorization", "Bearer $accessToken")' in risk


def test_android_risk_dashboard_has_no_fabricated_healthy_defaults():
    risk = (ROOT / "app" / "src" / "main" / "java" / "com" / "atlas" / "trading" / "ui" / "RiskDashboard.kt").read_text()
    assert "val equity: Double = 10000.0" not in risk
    assert "val currentDrawdownPct: Double = 4.30" not in risk
    assert "val sharpeRatio: Double = 1.84" not in risk
    assert "TRENDING LOW-VOL" not in risk
    assert 'streamType: String = "OFFLINE"' in risk
    assert "telemetryAvailable: Boolean = false" in risk
