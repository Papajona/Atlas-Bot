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



def test_database_role_guard_fails_closed_for_rls_incompatible_role():
    import pytest
    from app.rls_role_policy import require_rls_bypass_role

    with pytest.raises(RuntimeError, match="not authorized to bypass row-level security"):
        require_rls_bypass_role("atlas_app", is_superuser=False, bypass_rls=False)


def test_database_role_guard_accepts_superuser_bypassrls_or_table_owner_role():
    from app.rls_role_policy import require_rls_bypass_role

    require_rls_bypass_role("postgres", is_superuser=True, bypass_rls=False)
    require_rls_bypass_role("atlas_backend", is_superuser=False, bypass_rls=True)
    require_rls_bypass_role(
        "atlas_table_owner", is_superuser=False, bypass_rls=False, owns_all_rls_tables=True
    )


def test_rls_role_compatibility_guard_runs_before_database_initialization():
    src = (ROOT / "app" / "main.py").read_text()
    migration_check = src.index("await assert_database_migrations_current()")
    role_check = src.index("await assert_database_role_compatible_with_rls_lockdown()")
    database_init = src.index("await init_db()", role_check)
    assert migration_check < role_check < database_init


def test_public_schema_rls_guard_rejects_unprotected_or_empty_schema():
    import pytest
    from app.rls_role_policy import require_public_schema_rls_complete

    with pytest.raises(RuntimeError, match="RLS lockdown is incomplete"):
        require_public_schema_rls_complete(public_table_count=57, tables_without_rls=1)
    with pytest.raises(RuntimeError, match="RLS lockdown is incomplete"):
        require_public_schema_rls_complete(public_table_count=0, tables_without_rls=0)


def test_public_schema_rls_guard_accepts_all_tables_protected():
    from app.rls_role_policy import require_public_schema_rls_complete

    require_public_schema_rls_complete(public_table_count=57, tables_without_rls=0)

def test_production_deploy_wires_required_research_evidence_settings():
    workflow = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text()
    deploy = (ROOT / "deploy" / "cloud-run-deploy.sh").read_text()

    for name in (
        "RESEARCH_FEE_SOURCE",
        "RESEARCH_FEE_EVIDENCE_ID",
        "RESEARCH_MIN_DEFLATED_SHARPE",
    ):
        assert f"vars.{name}" in workflow
        assert f"${{{name}}}" in deploy
        assert f': "${{{name}:?' in deploy

    assert "threshold < 0.95" in deploy
    assert "@RESEARCH_FEE_SOURCE=" in deploy
    assert "@RESEARCH_FEE_EVIDENCE_ID=" in deploy
    assert "@RESEARCH_MIN_DEFLATED_SHARPE=" in deploy

def test_tron_ambiguous_deposit_uses_one_persistent_incident_without_reopening_resolved():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    funds = (ROOT / "app" / "customer_funds.py").read_text(encoding="utf-8")
    assert "TRON_DEPOSIT_IDENTITY_AMBIGUOUS:{wallet.id}:{txid}" in main
    assert "reopen_resolved=False" in main
    assert "async def record_ledger_incident(" in funds
    assert 'in {"RESOLVED", "CLOSED"}' in funds
    assert "row.resolved_at = None" in funds


def test_withdrawal_idempotency_key_is_payload_bound_and_sent_by_customer_ui():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    html = (ROOT / "app" / "templates" / "customer.html").read_text(encoding="utf-8")
    start = main.index("async def customer_create_withdrawal(")
    end = main.index('@app.get("/api/customer/withdrawals")', start)
    block = main[start:end]
    assert 'alias="Idempotency-Key"' in block
    assert "keyed_request_id" in block
    assert "Idempotency-Key was already used for a different withdrawal payload" in block
    assert block.index("keyed_existing") < block.index("_parse_stepup_token(")
    assert "'Idempotency-Key':idemKey" in html
    assert "atlas_withdrawal_idem_payload" in html


def test_tron_broadcast_result_cannot_overwrite_terminal_or_unknown_outcomes():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    start = main.index("async def admin_record_tron_sweep_broadcast(")
    end = main.index('@app.post("/api/admin/custody/tron/sweeps/{sweep_id}/reconcile")', start)
    block = main[start:end]
    assert "terminal_sweep_states" in block
    assert 'sweep.status != "READY_FOR_SIGNER"' in block
    assert "A rejection cannot override a prior timeout or known transaction" in block
    assert 'sweep.status == "SUBMITTED" and sweep.transaction_id == txid' in block


def test_production_deploy_requires_an_explicit_valid_administrator_bootstrap():
    deploy = (ROOT / "deploy" / "cloud-run-deploy.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(encoding="utf-8")
    assert ': "${ADMIN_ROLE_ASSIGNMENTS:?' in deploy
    assert "at least one explicit ADMINISTRATOR assignment is required" in deploy
    assert "every bootstrap role assignment must be present in ADMIN_SUPABASE_USER_IDS" in deploy
    assert "@ADMIN_ROLE_ASSIGNMENTS=${ADMIN_ROLE_ASSIGNMENTS}" in deploy
    assert "ADMIN_ROLE_ASSIGNMENTS: ${{ secrets.ADMIN_ROLE_ASSIGNMENTS }}" in workflow

def test_production_deploy_requires_verified_signed_android_artifact_for_exact_commit():
    android = (ROOT / ".github" / "workflows" / "android-release.yml").read_text(encoding="utf-8")
    deploy = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in android
    assert "github.ref == 'refs/heads/main'" in android
    assert "ATLAS_RELEASE_CERT_SHA256" in android
    assert "atlas-release-bundle" in android
    assert 'select(.name=="android-release")' in deploy
    assert "atlas-release-apk" in deploy
    assert "sha256sum --check atlas-release.sha256" in deploy
    assert "EXPECTED_CERT_SHA256" in deploy
    assert 'test "$package_name" = "com.atlas.trading"' in deploy


def test_deployment_independently_verifies_downloaded_apk_not_only_metadata():
    workflow = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(encoding="utf-8")
    block_start = workflow.index("Download and independently verify signed Android artifact")
    block_end = workflow.index("Authenticate to Google Cloud with GitHub OIDC", block_start)
    block = workflow[block_start:block_end]
    assert ' -name apksigner' in block
    assert ' -name aapt' in block
    assert 'verify --verbose --print-certs "$apk"' in block
    assert 'dump badging "$apk"' in block
    assert 'test "$actual_cert" = "$expected_cert"' in block
    assert 'test "$package_name" = "com.atlas.trading"' in block
    assert 'test "$metadata_package" = "$package_name"' in block
    assert 'test "$metadata_cert" = "$actual_cert"' in block
    assert 'test "$report_cert" = "$actual_cert"' in block
    assert 'expected certificate fingerprint must be 64 hex characters' in block


def test_deployment_selects_a_successful_signed_release_run_for_exact_sha():
    workflow = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(encoding="utf-8")
    block_start = workflow.index("Download and independently verify signed Android artifact")
    block_end = workflow.index("Authenticate to Google Cloud with GitHub OIDC", block_start)
    block = workflow[block_start:block_end]
    assert '--commit "$GITHUB_SHA"' in block
    assert 'select(.name=="android-release")' in block
    assert 'if [ "$job_conclusion" = "success" ]' in block
    assert '-ne 1' in block


def test_staging_identity_preflight_is_manual_main_only_and_read_only():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "environment: staging" in workflow
    assert "ATLAS_STAGING_SUPABASE_PROJECT_REF" in workflow
    assert "ATLAS_STAGING_DATABASE_SECRET" in workflow
    assert "ATLAS_STAGING_RUNTIME_DB_ROLE" in workflow
    assert "gcloud secrets versions access latest" in workflow
    assert "db.{expected}.supabase.co" in workflow
    assert "current_user, session_user" in workflow
    assert "default_transaction_read_only=on" in workflow
    assert "no migration or write was executed" in workflow
    assert "alembic upgrade" not in workflow


def test_staging_identity_preflight_does_not_confuse_identity_with_migration_readiness():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(encoding="utf-8")
    assert "IDENTITY PASS; MIGRATION BLOCKED" in workflow
    assert "expected before migration 0049" in workflow
    assert "no migration or write was executed" in workflow
    assert "alembic upgrade" not in workflow


def test_staging_preflight_owner_compatibility_matches_runtime_guard():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(encoding="utf-8")
    guard = (ROOT / "app" / "startup_guards.py").read_text(encoding="utf-8")
    assert "c.relrowsecurity AND NOT c.relforcerowsecurity" in workflow
    assert "c.relowner <> r.oid OR c.relforcerowsecurity" in guard
