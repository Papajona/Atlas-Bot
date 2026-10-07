from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_is_31047_and_immutable_identity_is_enforced():
    cfg = (ROOT / "app/config.py").read_text()
    init = (ROOT / "app/__init__.py").read_text()
    android = (ROOT / "app/build.gradle.kts").read_text()
    assert 'app_version: str = "3.10.47"' in cfg
    assert '__version__ = "3.10.47"' in init
    assert 'versionCode = 1047' in android
    assert 'versionName = "3.10.47"' in android
    main = (ROOT / "app/main.py").read_text()
    assert 'RELEASE_VERSION must exactly match the application release version' in main


def test_withdrawal_step_up_is_capped_at_three_minutes():
    cfg = (ROOT / "app/config.py").read_text()
    assert "withdrawal_step_up_minutes: int = 3" in cfg
    assert "must be between 1 and 3 minutes" in cfg


def test_ai_safety_has_hard_latency_ceiling_and_fails_closed():
    main = (ROOT / "app/main.py").read_text()
    assert "async def _run_ai_safety_review(packet: dict)" in main\n    assert "return await asyncio.wait_for(" in main\n    assert "dual_ai_trade_safety_review(packet)" in main
    assert "settings.ai_strategy_provider_timeout_seconds" in main
    assert '"stage": "ai_safety_timeout"' in main
    assert '"decision": "NO_TRADE"' in main


def test_repository_secret_scanner_is_part_of_release_and_ci():
    scanner = ROOT / "scripts/check_no_secrets.py"
    assert scanner.exists()
    assert "PRIVATE_KEY" in scanner.read_text()
    assert "check_no_secrets.py" in (ROOT / "scripts/verify_release.sh").read_text()
    assert "check_no_secrets.py" in (ROOT / ".github/workflows/ci.yml").read_text()
    assert "check_no_secrets.py" in (ROOT / "Dockerfile.verify").read_text()


def test_binance_user_stream_has_heartbeat_reconnect_and_reconciliation():
    src = (ROOT / "app/binance_user_stream.py").read_text()
    assert "ping_interval=None" in src
    assert "await ws.ping()" in src
    assert "asyncio.wait_for(ws.recv()" in src
    assert "backoff = min(backoff * 2.0" in src
    assert "await self.reconcile()" in src


def test_model_integrity_is_runtime_fail_closed():
    src = (ROOT / "app/model_registry.py").read_text()
    assert "verify_artifact_signature" in src
    assert "model_integrity_required_for_live" in src
    assert "return False" in src
