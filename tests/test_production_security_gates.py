from pathlib import Path
import os
import subprocess

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



def test_database_role_guard_fails_closed_for_rls_incompatible_or_privileged_roles():
    import pytest
    from app.rls_role_policy import require_rls_bypass_role

    with pytest.raises(RuntimeError, match="not authorized to bypass row-level security"):
        require_rls_bypass_role("atlas_app", is_superuser=False, bypass_rls=False)
    with pytest.raises(RuntimeError, match="not authorized to bypass row-level security"):
        require_rls_bypass_role("postgres", is_superuser=False, bypass_rls=True)
    with pytest.raises(RuntimeError, match="not authorized to bypass row-level security"):
        require_rls_bypass_role(
            "atlas_backend", is_superuser=False, bypass_rls=True, can_create_roles=True
        )
    with pytest.raises(RuntimeError, match="not authorized to bypass row-level security"):
        require_rls_bypass_role(
            "atlas_table_owner", is_superuser=False, bypass_rls=True, owned_public_table_count=57
        )


def test_database_role_guard_accepts_only_dedicated_non_owner_non_admin_bypass_role():
    from app.rls_role_policy import require_rls_bypass_role

    require_rls_bypass_role("atlas_backend", is_superuser=False, bypass_rls=True)

def test_database_role_guard_rejects_superuser_database_admin_and_reserved_service_roles():
    import pytest
    from app.rls_role_policy import require_rls_bypass_role

    invalid_roles = [
        {"role_name": "atlas_superuser", "is_superuser": True, "bypass_rls": True},
        {"role_name": "atlas_create_db", "is_superuser": False, "bypass_rls": True, "can_create_databases": True},
        {"role_name": "atlas_replication", "is_superuser": False, "bypass_rls": True, "can_replicate": True},
        {"role_name": "service_role", "is_superuser": False, "bypass_rls": True},
        {"role_name": "supabase_admin", "is_superuser": False, "bypass_rls": True},
        {"role_name": "atlas_inherited", "is_superuser": False, "bypass_rls": True, "role_membership_count": 1},
    ]
    for values in invalid_roles:
        role_name = values.pop("role_name")
        with pytest.raises(RuntimeError, match="not authorized to bypass row-level security"):
            require_rls_bypass_role(role_name, **values)




def test_rls_role_compatibility_guard_runs_before_database_initialization():
    src = (ROOT / "app" / "main.py").read_text()
    startup = (ROOT / "app" / "startup_guards.py").read_text(encoding="utf-8")
    assert "session_user AS login_role" in startup
    assert 'if str(row["login_role"]) != str(row["role_name"]):' in startup
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
    customer_js = (ROOT / "app" / "static" / "customer-dashboard.js").read_text(encoding="utf-8")
    html = html + customer_js
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
    assert "atlas-release-apk" in android
    assert 'select(.name=="android-release")' in deploy
    assert "atlas-release-apk" in deploy
    assert "sha256sum --check atlas-release.sha256" in deploy
    assert "EXPECTED_CERT_SHA256" in deploy
    assert "EXPECTED_CERT_SHA256: ${{ secrets.ATLAS_RELEASE_CERT_SHA256 }}" in deploy
    assert "EXPECTED_CERT_SHA256: ${{ vars.ATLAS_RELEASE_CERT_SHA256 }}" not in deploy
    assert 'test "$package_name" = "com.atlas.trading"' in deploy


def test_deployment_independently_verifies_downloaded_apk_not_only_metadata():
    workflow = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(encoding="utf-8")
    block_start = workflow.index("Download and independently verify selected Android artifact and backend origin")
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


def test_deployment_selects_an_explicit_successful_signed_release_run_for_exact_sha():
    workflow = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(encoding="utf-8")
    block_start = workflow.index("Require all release CI gates on this exact commit")
    block_end = workflow.index("Download and independently verify selected Android artifact and backend origin", block_start)
    block = workflow[block_start:block_end]
    assert "android_release_run_id:" in workflow
    assert 'android_run="$ANDROID_RELEASE_RUN_ID"' in block
    assert "run_sha" in block
    assert '"$run_sha" != "$GITHUB_SHA"' in block
    assert 'select(.name=="android-release")' in block
    assert 'if [ "$artifact_count" -ne 1 ]' in block
    assert "never select the first run by commit" in block
    assert "while IFS= read -r candidate" not in block


def test_staging_identity_preflight_is_manual_main_only_and_read_only():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    trigger_block = workflow.split("\npermissions:", 1)[0]
    assert "push:" not in trigger_block
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "environment: staging" in workflow
    assert "ATLAS_STAGING_SUPABASE_PROJECT_REF" in workflow
    assert "ATLAS_STAGING_DATABASE_SECRET" in workflow
    assert "ATLAS_STAGING_PAPER_DATABASE_SECRET" in workflow
    assert "ATLAS_STAGING_RUNTIME_DB_ROLE" in workflow
    assert "ATLAS_STAGING_PAPER_DB_ROLE" in workflow
    assert "gcloud secrets versions access latest" in workflow
    assert "db.{expected}.supabase.co" in workflow
    assert "current_user, session_user" in workflow
    assert "default_transaction_read_only=on" in workflow
    assert "no migration or write was executed" in workflow
    assert "alembic upgrade" not in workflow
    assert "app.paper_staging:app" in workflow
    assert "CUSTOMER_PAPER_DATABASE_URL" in workflow
    assert "ATLAS_STAGING_SUPABASE_ANON_KEY_SECRET" in workflow
    assert "service not found in the configured project/region" in workflow
    assert "roles/run.viewer" in workflow
    assert "raw output withheld" in workflow


def test_staging_identity_preflight_allows_same_project_and_checks_read_only_paper_runtime():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(encoding="utf-8")
    assert "ATLAS_PRODUCTION_SUPABASE_PROJECT_REF" in workflow
    assert "STAGING_SUPABASE_PROJECT_REF" in workflow
    assert "same-project supported" in workflow
    assert 'if [[ "$STAGING_SUPABASE_PROJECT_REF" == "$PRODUCTION_SUPABASE_PROJECT_REF" ]]; then' in workflow
    assert '[[ "$STAGING_SUPABASE_PROJECT_REF" != "$PRODUCTION_SUPABASE_PROJECT_REF" ]]' not in workflow
    assert "ATLAS_STAGING_CLOUD_RUN_SERVICE" in workflow
    assert "ATLAS_STAGING_RUNTIME_SERVICE_ACCOUNT" in workflow
    assert '"gcloud", "run", "services", "describe"' in workflow
    assert "DATABASE_URL must reference the configured atlas_paper_reader secret" in workflow
    assert "CUSTOMER_PAPER_DATABASE_URL must reference the configured atlas_customer_paper secret" in workflow
    assert "SUPABASE_ANON_KEY must reference the configured Supabase publishable/anon key secret" in workflow
    assert "primary DB URL is not the configured read-only runtime role" in workflow
    assert "session_role != role" in workflow
    assert "primary runtime role has administrative privileges or cannot login" in workflow
    assert "must be NOINHERIT/BYPASSRLS, with no inherited roles or unsafe Supabase-managed membership" in workflow
    assert "grantor_role.rolname='supabase_admin'" in workflow
    assert "to_jsonb(m) ? 'inherit_option'" in workflow
    assert "to_jsonb(m) ? 'set_option'" in workflow
    assert "no migration or write was executed" in workflow
    assert "can access only customer_profiles/app_state" in workflow
    assert "EXPECTED_ALEMBIC_HEAD" not in workflow
    assert "alembic upgrade" not in workflow


def test_staging_preflight_runtime_role_is_non_owner_and_non_admin():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(encoding="utf-8")
    guard = (ROOT / "app" / "startup_guards.py").read_text(encoding="utf-8")
    assert "owned_tables" in workflow
    assert "c.relowner=r.oid" in workflow
    assert "if any(int(v) for v in (other_schema, unexpected_tables, public_sequences, callable_definers, owned_tables))" in workflow
    assert "c.relowner = r.oid" in guard
    assert "r.rolcreaterole AS can_create_roles" in guard
    assert "r.rolcreatedb AS can_create_databases" in guard
    assert "pg_auth_members AS membership" in guard
    assert 'role_membership_count=int(row["role_membership_count"])' in guard


def test_production_deploy_checks_database_role_before_any_deployment():
    workflow = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(encoding="utf-8")
    check_start = workflow.index("Verify production database runtime role before deployment")
    deploy_start = workflow.index("Deploy immutable API release", check_start)
    block = workflow[check_start:deploy_start]
    assert 'gcloud secrets versions access "$SECRET_DATABASE_VERSION"' in block
    assert "default_transaction_read_only=on" in block
    assert "current_database(), current_user, session_user" in block
    assert "pg_auth_members" in block
    assert "BLOCKED: production runtime cannot use a reserved" in block
    assert "BLOCKED: production runtime role must own no public tables and inherit no other roles" in block
    assert 'print("BLOCKED: read-only production database role check failed; database output withheld")' in block
    assert 'db_url_file="$RUNNER_TEMP/atlas-production-db-url"' in block
    assert "trap 'rm -f \"$db_url_file\"' EXIT" in block

def test_production_deploy_requires_successful_staging_identity_job_for_exact_main_commit():
    workflow = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(encoding="utf-8")
    assert "require_staging_identity_success" in workflow
    assert 'require_success "staging-identity-preflight.yml" "Supabase staging identity preflight"' not in workflow
    assert '--workflow "staging-identity-preflight.yml"' in workflow
    assert '--commit "$GITHUB_SHA"' in workflow
    assert 'select(.headBranch == "main")' in workflow
    assert 'select(.name == "verify-staging-identity")][0].conclusion // "missing"' in workflow
    assert '[ "$job_conclusion" != "success" ]' in workflow


def test_production_startup_smoke_uses_non_admin_runtime_role_fixture():
    workflow = (ROOT / ".github" / "workflows" / "production-verification.yml").read_text(encoding="utf-8")
    fixture_start = workflow.index("Create disposable least-privilege runtime-role fixture")
    smoke_start = workflow.index("Production startup smoke test", fixture_start)
    block = workflow[fixture_start:smoke_start]
    assert "CREATE ROLE atlas_runtime LOGIN" in block
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS" in block
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in block
    assert "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES" in block
    smoke_end = workflow.index("Full regression suite in safe mode", smoke_start)
    smoke = workflow[smoke_start:smoke_end]
    assert "DATABASE_URL: postgresql+asyncpg://atlas_runtime:" in smoke
    assert "postgresql+asyncpg://atlas:verification_password" not in smoke


def test_production_verification_blocks_missing_provider_credentials():
    workflow = (ROOT / ".github" / "workflows" / "production-verification.yml").read_text(encoding="utf-8")
    assert "BLOCKED: provider credentials are not configured" in workflow
    assert "this is not a provider verification pass" not in workflow


def test_production_verification_passes_provider_secrets_to_checker():
    workflow = (ROOT / ".github" / "workflows" / "production-verification.yml").read_text(encoding="utf-8")
    block_start = workflow.index("AI model/provider verification when credentials are supplied")
    block_end = workflow.index("Production-oriented static and security gates", block_start)
    block = workflow[block_start:block_end]
    assert 'GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY || secrets.GEMINI }}' in block
    assert 'GROQ_API_KEY: ${{ secrets.GROQ_API_KEY || secrets.GROQ }}' in block
    assert 'CEREBRAS_API_KEY: ${{ secrets.CEREBRAS_API_KEY }}' in block
    assert "python -m scripts.check_ai_models" in block
    assert "temporarily waived" not in block
    assert "BLOCKED: provider credentials are not configured" in block


def test_supabase_future_default_grants_are_opt_in():
    migration = (
        ROOT / "alembic" / "versions" / "0053_supabase_public_default_acl_lockdown.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "0053_supabase_public_default_acl_lockdown"' in migration
    assert 'down_revision = "0052_schema_metadata_reconciliation"' in migration
    assert (
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE ALL PRIVILEGES ON TABLES FROM service_role"
    ) in migration
    assert (
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE ALL PRIVILEGES ON SEQUENCES FROM service_role"
    ) in migration
    assert (
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE EXECUTE ON FUNCTIONS FROM service_role"
    ) in migration
    # Existing-object grants must be left intact; only defaults for future objects change.
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES" not in migration
    assert "REVOKE ALL PRIVILEGES ON ALL SEQUENCES" not in migration

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "alembic upgrade 0053_supabase_public_default_acl_lockdown" in workflow
    assert "migration unexpectedly removed service_role access from an existing table" in workflow
    assert "has_table_privilege('service_role', 'public.atlas_default_acl_probe', 'SELECT')" in workflow

def test_paper_staging_deployment_is_restricted_and_keeps_risk_features_off():
    deploy = (ROOT / ".github" / "workflows" / "staging-paper-deploy.yml").read_text(encoding="utf-8")
    preflight = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(encoding="utf-8")
    paper_app = (ROOT / "app" / "paper_staging.py").read_text(encoding="utf-8")
    assert "github.ref == 'refs/heads/main'" in deploy
    assert "ATLAS_ASGI_APP=app.paper_staging:app" in deploy
    for name, value in (
        ("PAPER_TRADING", "true"),
        ("LIVE_TRADING_ENABLED", "false"),
        ("CUSTOMER_LIVE_TRADING_ENABLED", "false"),
        ("PAYOUT_LIVE_ENABLED", "false"),
        ("BROKER_SANDBOX", "true"),
        ("USDT_TRON_ENABLED", "false"),
        ("USDT_TRON_SWEEP_ENABLED", "false"),
        ("DERIV_LIVE_ENABLED", "false"),
        ("CUSTOMER_PAPER_LEDGER_ENABLED", "true"),
        ("CUSTOMER_PAPER_SHARED_DATABASE_ENABLED", "true"),
        ("CUSTOMER_TOTP_REQUIRED", "true"),
        ("ADAPTIVE_AI_ENABLED", "false"),
    ):
        assert f"{name}={value}" in deploy
        assert f'"{name}": "{value}"' in preflight
    assert "atlas-staging-primary-db-url" not in deploy
    assert "atlas-staging-customer-paper-db-url" not in deploy
    assert "atlas-staging-supabase-anon-key" not in deploy
    assert "set-secrets" in deploy
    assert "current_setting('transaction_read_only')" in paper_app
    assert 'app.middleware("http")(api_rate_limit)' in paper_app
    assert '"transaction_read_only"] or "").lower() not in {"on", "true", "t"}' in paper_app
    assert "Customer profile must already exist" in paper_app
    assert "app.main:app" not in deploy



def test_staging_deployment_is_marker_gated_and_waits_for_exact_commit_gates():
    deploy = (ROOT / ".github" / "workflows" / "staging-paper-deploy.yml").read_text(encoding="utf-8")
    trigger_header = deploy.split("\npermissions:", 1)[0]
    assert "workflow_dispatch:" in trigger_header
    assert "push:" in trigger_header
    assert "branches:\n      - main" in trigger_header
    assert "contains(github.event.head_commit.message, '[atlas-paper-staging-deploy-once-20261023]')" in deploy
    assert "github.ref == 'refs/heads/main'" in deploy
    assert "Wait for required CI gates to pass on this exact commit" in deploy
    assert '--commit "$GITHUB_SHA"' in deploy
    for required_workflow in ("ci.yml", "ledger-concurrency-check.yml", "production-verification.yml"):
        assert f'"{required_workflow}"' in deploy
    assert "gh workflow run staging-identity-preflight.yml" in deploy
    assert '-f "request_id=$request_id"' in deploy
    assert 'gh run view "$preflight_run_id"' in deploy
    assert "select(.displayTitle == $expected)" in deploy
    assert "uniquely correlated staging preflight run was not observed" in deploy
    assert "main advanced during staging preflight" in deploy
    assert "timeout-minutes: 60" in deploy
    assert "actions: write" in deploy


def test_staging_workflows_are_valid_yaml_with_the_expected_named_jobs():
    ruby = r"""
require "yaml"
deploy = YAML.load_file(ARGV.fetch(0))
preflight = YAML.load_file(ARGV.fetch(1))
abort("paper staging deploy job missing") unless deploy.fetch("jobs").key?("deploy-paper-staging")
abort("staging identity preflight job missing") unless preflight.fetch("jobs").key?("verify-staging-identity")
"""
    result = subprocess.run(
        [
            "ruby", "-e", ruby,
            str(ROOT / ".github" / "workflows" / "staging-paper-deploy.yml"),
            str(ROOT / ".github" / "workflows" / "staging-identity-preflight.yml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout

def test_paper_reader_provisioning_is_no_write_and_no_public_schema_create():
    sql = (ROOT / "scripts" / "provision_atlas_paper_reader.sql").read_text(encoding="utf-8")
    assert "CREATE ROLE atlas_paper_reader" in sql
    assert "LOGIN NOINHERIT BYPASSRLS" in sql
    assert "GRANT SELECT ON TABLE public.customer_profiles, public.app_state" in sql
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM atlas_paper_reader" in sql
    assert "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM atlas_paper_reader" in sql
    assert "ALTER ROLE atlas_paper_reader SET default_transaction_read_only = on" in sql
    assert "GRANT INSERT" not in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql



def test_staging_identity_preflight_is_dispatched_after_paper_service_deployment():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(encoding="utf-8")
    trigger_header = workflow.split("\npermissions:", 1)[0]
    assert "workflow_dispatch:" in trigger_header
    assert "\n  push:" not in trigger_header
    assert "verify-staging-identity:" in workflow
    deploy = (ROOT / ".github" / "workflows" / "staging-paper-deploy.yml").read_text(encoding="utf-8")
    assert "gh workflow run staging-identity-preflight.yml" in deploy
    assert deploy.index("Deploy restricted paper-only Cloud Run service") < deploy.index("Dispatch and require read-only staging identity preflight")


def _run_production_staging_job_gate(tmp_path: Path, job_conclusion: str):
    workflow = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(encoding="utf-8")
    start = workflow.index("          require_staging_identity_success() {")
    end = workflow.index("\n          require_staging_identity_success\n", start)
    shell_source = workflow[start:end + len("\n          require_staging_identity_success")]
    assert start >= 0 and end >= 0, "production workflow must expose the exact staging job gate"

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gh = fake_bin / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ \"$2\" == \"list\" ]]; then echo 123; exit 0; fi\n"
        "if [[ \"$2\" == \"view\" ]]; then\n"
        "  if [[ \"$*\" == *\"--json status\"* ]]; then echo completed; exit 0; fi\n"
        "  if [[ \"$*\" == *\"--json conclusion\"* ]]; then echo success; exit 0; fi\n"
        "  if [[ \"$*\" == *\"--json jobs\"* ]]; then echo \"$STAGING_JOB_CONCLUSION\"; exit 0; fi\n"
        "fi\n"
        "echo unexpected-gh-invocation >&2\n"
        "exit 70\n",
        encoding="utf-8",
    )
    gh.chmod(0o700)
    shell_block = "\n".join(
        line[10:] if line.startswith("          ") else line
        for line in shell_source.splitlines()
    )
    result = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + shell_block],
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "GITHUB_REPOSITORY": "Papajona/Atlas-Bot",
            "GITHUB_SHA": "f" * 40,
            "STAGING_JOB_CONCLUSION": job_conclusion,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result


def test_production_staging_gate_rejects_successful_workflow_with_skipped_job(tmp_path: Path):
    result = _run_production_staging_job_gate(tmp_path, "skipped")

    assert result.returncode != 0
    assert "requires verify-staging-identity to succeed" in result.stdout


def test_production_staging_gate_accepts_successful_exact_commit_job(tmp_path: Path):
    result = _run_production_staging_job_gate(tmp_path, "success")

    assert result.returncode == 0, result.stderr
    assert "verify-staging-identity succeeded" in result.stdout


def test_production_deploy_workflow_is_valid_yaml_with_unique_named_steps():
    workflow_path = ROOT / ".github" / "workflows" / "production-deploy.yml"
    ruby = r"""
require "yaml"
workflow = YAML.load_file(ARGV.fetch(0))
steps = workflow.fetch("jobs").values.flat_map { |job| job.fetch("steps", []) }
names = steps.map { |step| step["name"] }.compact
duplicates = names.group_by(&:itself).select { |_, occurrences| occurrences.length > 1 }.keys
abort("duplicate production deployment step names: #{duplicates.join(', ')}") unless duplicates.empty?
abort("exact staging identity job gate missing") unless names.include?("Require all release CI gates on this exact commit")
"""
    result = subprocess.run(
        ["ruby", "-e", ruby, str(workflow_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
