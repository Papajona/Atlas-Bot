import asyncio
from pathlib import Path

from app import main


ROOT = Path(__file__).resolve().parents[1]


def test_customer_profile_backfill_encrypts_auth_pii_and_checks_legacy_identity():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    block = source[
        source.index("async def _legacy_customer_email_conflict"):
        source.index("def _funding_signature_valid")
    ]
    assert "_supabase_user(_bearer_token(authorization))" in block
    assert 'user.get("email_confirmed_at")' in block
    assert "profile.email = email" in block
    assert "profile.display_name = name" in block
    assert "PENDING_IDENTITY_REVIEW" in block
    assert "_legacy_customer_email_conflict(db, uid, email)" in block
    assert "casefold()" in block


def test_legacy_email_duplicate_detection_uses_decrypted_orm_values_not_sql_like():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    block = source[
        source.index("async def _legacy_customer_email_conflict"):
        source.index("async def get_customer")
    ]
    assert "select(CustomerProfile.auth_user_id, CustomerProfile.email)" in block
    assert "uuid.UUID" in block
    assert ".ilike(" not in block


def test_edge_provisioning_keeps_pii_encrypted_and_paper_portfolio_is_guarded():
    edge = (ROOT / "supabase/functions/customer-profile-provision/index.ts").read_text(encoding="utf-8")
    insert = edge[edge.index(".insert({"):edge.index("})", edge.index(".insert({")) + 2]
    assert "auth_user_id: userId" in insert
    assert "email:" not in insert
    assert "display_name:" not in insert

    paper = (ROOT / "app/paper_staging.py").read_text(encoding="utf-8")
    portfolio = paper[
        paper.index("async def paper_customer_portfolio"):
        paper.index("app.include_router")
    ]
    assert "await _paper_state_guard()" in portfolio
    summary = paper[
        paper.index("async def paper_customer_me"):
        paper.index('@app.get("/api/customer/portfolio")')
    ]
    assert "_supabase_user(_bearer_token(authorization))" in summary
    assert "CustomerProfile.email" not in summary
    assert "CustomerProfile.display_name" not in summary


def test_profile_conflict_returns_actionable_409_not_auth_outage():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    helper = source[source.index("async def _supabase_request"):source.index("async def _supabase_user")]
    assert "response.status_code == 409" in helper
    assert '"/functions/v1/customer-profile-provision"' in helper
    assert "Manual identity review is required" in helper
    assert "raise HTTPException(" in helper


def test_risk_dashboard_uses_only_latest_closed_platform_outcome_per_trade():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert source.count("latest_closed = (") == 2
    assert source.count("StrategyOutcome.customer_id.is_(None)") >= 4
    assert source.count('StrategyOutcome.lifecycle_status == "CLOSED"') >= 4
    ratios = (ROOT / "app/risk_metrics.py").read_text(encoding="utf-8")
    assert "MIN_ANNUALIZATION_SPAN_SECONDS = 30 * 86400" in ratios
    assert "interval_count = len(times) - 1" in ratios


def test_risk_websocket_publishes_annualization_and_sample_metadata():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    websocket = source[
        source.index("async def websocket_risk_telemetry"):
        source.index('@app.get("/api/trades")')
    ]
    assert '"ratios_annualized": ratios["annualized"]' in websocket
    assert '"ratio_sample_size": ratios["sample_size"]' in websocket


def test_strategy_candidate_maps_invalid_symbol_to_422():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    route = source[
        source.index('@app.post("/api/customer/strategy-lab/candidates")'):
        source.index("def _candidate_backtest_config")
    ]
    assert "except NativeAIInputError" in route
    assert "422" in route


def test_android_can_sign_in_with_short_existing_password_and_recover_after_totp():
    source = (ROOT / "app/src/main/java/com/atlas/trading/ui/UserDashboardScreen.kt").read_text(encoding="utf-8")
    assert "if (creatingAccount) password.length >= 8 else password.isNotBlank()" in source
    verify = source[
        source.index("onVerifyAuthenticator = { code ->"):
        source.index("onVerifyAuthenticator = { code ->") + 1800
    ]
    assert verify.index("customerMfaChallenge = null") < verify.index("auth.provisionProfile")
    assert verify.index("customerMfaSetupSecret = null") < verify.index("auth.provisionProfile")


def test_stale_pending_totp_factor_is_deleted_before_reenrollment():
    source = (ROOT / "app/paper_staging.py").read_text(encoding="utf-8")
    enroll = source[
        source.index("async def paper_customer_mfa_enroll"):
        source.index('@app.post("/api/auth/mfa/challenge")')
    ]
    assert 'factor_type") or "").lower() != "totp"' in enroll
    assert 'factor.get("status") or "").lower() != "unverified"' in enroll
    assert 'method="DELETE"' in enroll
    assert enroll.index('method="DELETE"') < enroll.index('payload={"factor_type": "totp"')



def test_unmapped_legacy_identity_fails_closed_for_new_profiles():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    helper = source[
        source.index("async def _legacy_customer_email_conflict"):
        source.index("async def get_customer")
    ]
    assert "if not str(stored_email or "").strip():" in helper
    assert "return True" in helper

    edge = (ROOT / "supabase/functions/customer-profile-provision/index.ts").read_text(encoding="utf-8")
    provisioning = edge[
        edge.index("const profiles = ctx.supabaseAdmin.from"):
        edge.index("const now = new Date().toISOString()")
    ]
    assert "ALREADY_PROVISIONED" in provisioning
    assert "hasUnmappedLegacyIdentity" in provisioning
    assert "New customer provisioning is paused" in provisioning
    assert "}, 409)" in provisioning



def test_unmapped_legacy_profile_with_blank_email_blocks_new_identity():
    class Result:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

    class Database:
        def __init__(self, rows):
            self.rows = rows

        async def execute(self, _statement):
            return Result(self.rows)

    legacy = asyncio.run(
        main._legacy_customer_email_conflict(
            Database([("legacy-tron-reference", "")]),
            "new-supabase-uuid",
            "new@example.invalid",
        )
    )
    unrelated_uuid = asyncio.run(
        main._legacy_customer_email_conflict(
            Database([("8e9c7e76-f8b6-4caa-96c4-0e451f4e2fb7", "")]),
            "new-supabase-uuid",
            "new@example.invalid",
        )
    )
    assert legacy is True
    assert unrelated_uuid is False



def test_core_customer_summary_can_show_own_auth_pii_during_review_without_unlocking_customer_access():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    summary = source[
        source.index('@app.get("/api/customer/me")'):
        source.index('@app.get("/api/customer/wallets")')
    ]
    assert "allow_identity_review_summary=True" in summary
    assert '"identity_review_required": profile.status == "PENDING_IDENTITY_REVIEW"' in summary

    get_customer = source[
        source.index("async def get_customer("):
        source.index("def _funding_signature_valid")
    ]
    assert "if identity_review_required and not allow_identity_review_summary" in get_customer
    assert "if not identity_review_required:" in get_customer
    assert "allow_identity_review_summary" in get_customer
    assert "PENDING_IDENTITY_REVIEW" in get_customer


def test_paper_customer_access_fails_closed_while_legacy_auth_references_remain():
    source = (ROOT / "app/paper_staging.py").read_text(encoding="utf-8")
    gate = source[
        source.index("async def get_existing_customer"):
        source.index("async def _paper_state_guard")
    ]
    assert "legacy_auth_refs" in gate
    assert "uuid.UUID" in gate
    assert "Paper access is paused until legacy customer identities are manually reviewed" in gate



def test_paper_customer_summary_is_read_only_and_marks_identity_review():
    source = (ROOT / "app/paper_staging.py").read_text(encoding="utf-8")
    summary = source[
        source.index("async def paper_customer_me"):
        source.index('@app.get("/api/customer/portfolio")')
    ]
    assert "allow_identity_review_summary=True" in summary
    assert '"identity_review_required": bool(profile.identity_review_required)' in summary

    access = source[
        source.index("async def get_existing_customer"):
        source.index("async def _paper_state_guard")
    ]
    assert "if not allow_identity_review_summary:" in access
    assert "identity_review_required=identity_review_required" in access
