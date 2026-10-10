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
    assert "WebSocketManager" in risk
    assert "fetchLiveRiskMetrics" not in risk
    assert "HttpURLConnection" not in risk


def test_android_risk_dashboard_has_no_fabricated_healthy_defaults():
    risk = (ROOT / "app" / "src" / "main" / "java" / "com" / "atlas" / "trading" / "ui" / "RiskDashboard.kt").read_text()
    assert "val equity: Double = 10000.0" not in risk
    assert "val currentDrawdownPct: Double = 4.30" not in risk
    assert "val sharpeRatio: Double = 1.84" not in risk
    assert "TRENDING LOW-VOL" not in risk
    assert 'streamType: String = "OFFLINE"' in risk
    assert "telemetryAvailable: Boolean = false" in risk


def test_worker_health_and_local_security_audit_are_wired():
    main = (ROOT / "app" / "main.py").read_text()
    audit = (ROOT / "app" / "src" / "main" / "java" / "com" / "atlas" / "trading" / "security" / "SecurityAuditLog.kt").read_text()
    activity = (ROOT / "app" / "src" / "main" / "java" / "com" / "atlas" / "trading" / "MainActivity.kt").read_text()
    risk = (ROOT / "app" / "src" / "main" / "java" / "com" / "atlas" / "trading" / "ui" / "RiskDashboard.kt").read_text()
    manager = (ROOT / "app" / "src" / "main" / "java" / "com" / "atlas" / "trading" / "network" / "WebSocketManager.kt").read_text()
    assert "_worker_health_snapshot" in main
    assert "/api/risk-dashboard/worker-health" in main
    assert '"worker_health": worker_health' in main
    assert "AndroidKeyStore" in audit
    assert "AES/GCM/NoPadding" in audit
    assert "noBackupFilesDir" in audit
    assert "BIOMETRIC_AUTH_FAILED" in activity
    assert "RISK_KILL_SWITCH_CONFIRMED" in activity
    assert "worker_health_status" in risk
    assert "scheduleReconnect" in manager
    assert "initiateConnection(wsUrl, token)" in manager


def test_production_deploy_defaults_tron_funding_and_sweep_off():
    deploy = (ROOT / "deploy" / "cloud-run-deploy.sh").read_text(encoding="utf-8")
    assert 'source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/tron-funding-gate.sh"' in deploy
    assert "atlas_configure_tron_funding" in deploy
    assert "@USDT_TRON_ENABLED=${USDT_TRON_ENABLED}" in deploy
    assert "@USDT_TRON_NETWORK=${USDT_TRON_NETWORK}" in deploy
    assert "@USDT_TRON_SWEEP_ENABLED=false" in deploy
    assert "@USDT_TRON_ENABLED=true" not in deploy


def test_tron_funding_gate_defaults_off_and_requires_both_explicit_approvals():
    import os
    import subprocess

    gate = ROOT / "deploy" / "tron-funding-gate.sh"
    command = (
        f'source "{gate}"; '
        'atlas_configure_tron_funding; '
        'printf "%s|%s" "$USDT_TRON_ENABLED" "$USDT_TRON_NETWORK"'
    )
    base_env = os.environ.copy()
    for name in (
        "USDT_TRON_ENABLED",
        "USDT_TRON_NETWORK",
        "ALLOW_TRON_FUNDING",
        "ALLOW_MAINNET_TRON_FUNDING",
    ):
        base_env.pop(name, None)

    default = subprocess.run(
        ["bash", "-c", command], env=base_env, capture_output=True, text=True, check=False
    )
    assert default.returncode == 0, default.stderr
    assert default.stdout == "false|mainnet"

    enabled_without_approval = base_env | {"USDT_TRON_ENABLED": "true"}
    blocked = subprocess.run(
        ["bash", "-c", command],
        env=enabled_without_approval,
        capture_output=True,
        text=True,
        check=False,
    )
    assert blocked.returncode != 0
    assert "TRON funding is disabled by default" in blocked.stderr

    enabled_with_one_approval = enabled_without_approval | {"ALLOW_TRON_FUNDING": "YES"}
    blocked_mainnet = subprocess.run(
        ["bash", "-c", command],
        env=enabled_with_one_approval,
        capture_output=True,
        text=True,
        check=False,
    )
    assert blocked_mainnet.returncode != 0
    assert "mainnet TRON funding requires" in blocked_mainnet.stderr

    fully_approved = enabled_with_one_approval | {"ALLOW_MAINNET_TRON_FUNDING": "YES"}
    allowed = subprocess.run(
        ["bash", "-c", command], env=fully_approved, capture_output=True, text=True, check=False
    )
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout == "true|mainnet"
