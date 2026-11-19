import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app import main


ROOT = Path(__file__).resolve().parents[1]


def test_customer_profile_backfill_uses_auth_claims_without_name_only_network_calls():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    block = source[source.index("async def get_customer("):source.index("def _funding_signature_valid")]
    assert "_supabase_user(_bearer_token(authorization))" in block
    assert 'user.get("email_confirmed_at")' in block
    assert "profile.email = email" in block
    assert "profile.display_name = name" in block
    assert "if not profile.display_name and name:" in block
    assert "if not str(profile.display_name" not in block
    assert 'profile.status = "PENDING_IDENTITY_REVIEW"' not in block
    assert "has_unmapped_legacy_customer_identity(db)" in block


def test_legacy_identity_detection_is_bounded_and_dialect_aware():
    source = (ROOT / "app/customer_identity.py").read_text(encoding="utf-8")
    assert 'column.op("!~*")' in source
    assert 'func.lower(column).op("NOT GLOB")' in source
    assert "literal_column(" in source
    migration = (ROOT / "alembic/versions/0060_customer_identity_unmapped_index.py").read_text(encoding="utf-8")
    assert "ix_customer_profiles_unmapped_auth_user_id" in migration
    assert "auth_user_id !~*" in migration
    assert "ix_strategy_outcome_platform_closed_trade_time" in migration
    assert ".limit(1)" in source
    assert "rows = (await db.execute(statement)).scalars().all()" in source


def test_edge_provisioning_keeps_pii_encrypted_and_pages_legacy_rows_stably():
    edge = (ROOT / "supabase/functions/customer-profile-provision/index.ts").read_text(encoding="utf-8")
    insert = edge[edge.index(".insert({"):edge.index("})", edge.index(".insert({")) + 2]
    assert "auth_user_id: userId" in insert
    assert "email:" not in insert
    assert "display_name:" not in insert
    page = edge[edge.index('.select("auth_user_id")'):edge.index("if (legacyLookupError")]
    assert '.order("id", { ascending: true })' in page
    assert page.index('.order("id", { ascending: true })') < page.index(".range(")


def test_paper_portfolio_authenticates_before_state_guard_and_never_scans_all_profiles():
    paper = (ROOT / "app/paper_staging.py").read_text(encoding="utf-8")
    portfolio = paper[
        paper.index("async def paper_customer_portfolio"):
        paper.index("app.include_router")
    ]
    assert portfolio.index("await get_existing_customer") < portfolio.index("await _paper_state_guard()")
    assert portfolio.index("await _paper_state_guard()") < portfolio.index("get_customer_paper_balances")
    gate = paper[
        paper.index("async def get_existing_customer"):
        paper.index("async def _paper_state_guard")
    ]
    assert "has_unmapped_legacy_customer_identity(db)" in gate
    assert "legacy_auth_refs" not in gate
    assert ".scalars().all()" not in gate


def test_profile_conflict_returns_actionable_409_not_auth_outage():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    helper = source[source.index("async def _supabase_request"):source.index("async def _supabase_admin_user")]
    assert "response.status_code == 409" in helper
    assert '"/functions/v1/customer-profile-provision"' in helper
    assert "Manual identity review is required" in helper
    assert "raise HTTPException(" in helper


def test_withdrawal_review_resolves_empty_encrypted_email_from_auth_admin():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    admin_user = source[source.index("async def _supabase_admin_user"):source.index("async def _supabase_user")]
    route = source[source.index('@app.get("/api/admin/withdrawals")'):source.index('@app.post("/api/admin/withdrawals")')]
    assert "settings.supabase_auth_admin_key" in admin_user
    assert '"/auth/v1/admin/users/' in admin_user
    assert "response.status_code == 404" in admin_user
    assert 'str(user.get("id") or "") != user_id' in admin_user
    assert "_resolve_withdrawal_auth_email(auth_id)" in route
    assert '"customer_email_status": email_status' in route
    assert "AUTH_LOOKUP_NOT_CONFIGURED" in route
    assert "AUTH_LOOKUP_UNAVAILABLE" in route
    assert "AUTH_LOOKUP_DEFERRED" in route
    assert "LEGACY_IDENTITY_UNMAPPED" in route
    assert "asyncio.wait_for(" in route
    assert "missing_auth_ids = {" in route
    assert route.index("snapshot.append({") < route.index("missing_auth_ids = {")
    assert "_WITHDRAWAL_AUTH_LOOKUP_MAX_PER_REQUEST = 50" in source
    assert "_WITHDRAWAL_AUTH_EMAIL_CACHE_TTL_SECONDS = 300.0" in source
    assert "_cached_withdrawal_auth_email" in source


def test_risk_dashboard_uses_bounded_recent_trade_candidates_in_both_transports():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert source.count("_recent_closed_platform_trade_returns(db)") == 2
    assert "latest_closed = (" not in source
    assert "_RISK_OUTCOME_SAMPLE_SIZE = 100" in source
    assert ".limit(_RISK_OUTCOME_SAMPLE_SIZE)" in source
    assert ".group_by(StrategyOutcome.trade_id)" in source
    assert "func.row_number().over(" in source
    assert "_RISK_OUTCOME_MAX_FILL_ROWS_PER_TRADE = 250" in source
    assert "ranked_fills.c.fill_rank <= _RISK_OUTCOME_MAX_FILL_ROWS_PER_TRADE + 1" in source
    assert "incomplete_trade_ids" in source
    assert "ratio_sample_incomplete_trades" in source


def test_partial_close_fills_are_weighted_and_use_final_close_time():
    start = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
    rows = [
        SimpleNamespace(
            trade_id=41, metadata_json=json.dumps({"closing_notional": "1000"}),
            realized_pnl="10", return_bps=100, net_pnl="10", created_at=start,
        ),
        SimpleNamespace(
            trade_id=41, metadata_json=json.dumps({"closing_notional": "3000"}),
            realized_pnl="30", return_bps=100, net_pnl="30",
            created_at=start + timedelta(minutes=5),
        ),
        # A zero-PnL closing fill still contributes its notional and fees.
        SimpleNamespace(
            trade_id=41, metadata_json=json.dumps({"closing_notional": "1000"}),
            realized_pnl="0", return_bps=0, net_pnl="-1",
            created_at=start + timedelta(minutes=10),
        ),
    ]
    result = main._aggregate_closed_outcome_fills(rows)
    assert len(result) == 1
    assert result[0][0] == 0.0078
    assert result[0][1] == start + timedelta(minutes=10)


def test_legacy_outcome_notional_can_be_reconstructed_without_new_metadata():
    start = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
    row = SimpleNamespace(
        trade_id=42, metadata_json="{}", realized_pnl="5",
        return_bps=50, net_pnl="4", created_at=start,
    )
    result = main._aggregate_closed_outcome_fills([row])
    assert result == [(0.004, start)]


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


def test_new_customer_profiles_fail_closed_while_legacy_identities_remain():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    get_customer = source[
        source.index("async def get_customer("):
        source.index("def _funding_signature_valid")
    ]
    assert "has_unmapped_legacy_customer_identity(db)" in get_customer
    assert "if profile is None and identity_review_required:" in get_customer
    assert 'profile.status = "PENDING_IDENTITY_REVIEW"' not in get_customer

    edge = (ROOT / "supabase/functions/customer-profile-provision/index.ts").read_text(encoding="utf-8")
    provisioning = edge[
        edge.index("const profiles = ctx.supabaseAdmin.from"):
        edge.index("const now = new Date().toISOString()")
    ]
    assert "ALREADY_PROVISIONED" in provisioning
    assert "hasUnmappedLegacyIdentity" in provisioning
    assert "New customer provisioning is paused" in provisioning
    assert "}, 409)" in provisioning


def test_core_customer_summary_reports_review_without_permanently_quarantining():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    summary = source[
        source.index('@app.get("/api/customer/me")'):
        source.index('@app.get("/api/customer/wallets")')
    ]
    assert "allow_identity_review_summary=True" in summary
    assert '"identity_review_required": bool(getattr(profile, "identity_review_required", False))' in summary

    get_customer = source[
        source.index("async def get_customer("):
        source.index("def _funding_signature_valid")
    ]
    assert "if identity_review_required and not allow_identity_review_summary" in get_customer
    assert 'profile.status = "ACTIVE"' in get_customer
    assert "PENDING_IDENTITY_REVIEW" in get_customer


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
    assert "if identity_review_required and not allow_identity_review_summary:" in access
    assert "identity_review_required=identity_review_required" in access


def test_existing_active_customer_is_not_blocked_by_an_unrelated_legacy_identity():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    block = source[
        source.index("async def get_customer("):
        source.index("def _funding_signature_valid")
    ]
    assert "profile_needs_identity_review = (" in block
    assert 'str(profile.status or "").upper() == "PENDING_IDENTITY_REVIEW"' in block
    assert 'or not str(profile.email or "").strip()' in block
    assert "if profile_needs_identity_review else False" in block
    assert "if profile is None and identity_review_required:" in block


def test_existing_profile_auth_backfill_never_persists_unconfirmed_email():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    block = source[
        source.index("async def get_customer("):
        source.index("def _funding_signature_valid")
    ]
    assert 'if user.get("email_confirmed_at"):' in block
    assert 'logger.info("customer_auth_pii_backfill_unconfirmed' in block
    assert "email = str(user.get(\"email\") or \"\").strip().lower()[:320]" in block


def test_same_direction_position_additions_have_a_defined_zero_close_quantity():
    from app import execution

    source = (ROOT / "app/execution.py").read_text(encoding="utf-8")
    block = source[
        source.index("async def _apply_fill_to_position"):
        source.index("async def _release_unneeded_order_reserve")
    ]
    assert block.index("closing = 0.0") < block.index("if not position:")
    assert "if closing > 0:" in block


def test_live_and_paper_partial_close_fees_use_only_the_incremental_fill_fee():
    from app.execution import _closing_fill_fee_share

    assert _closing_fill_fee_share(Decimal("3"), closing_quantity=1, fill_quantity=3) == Decimal("1")
    assert _closing_fill_fee_share(Decimal("2"), closing_quantity=2, fill_quantity=2) == Decimal("2")
    # Two broker snapshots report cumulative fee totals 3 then 5. The outcome
    # ledger must charge the deltas 3 then 2, not the cumulative 3 then 5.
    assert (
        _closing_fill_fee_share(Decimal("3"), closing_quantity=3, fill_quantity=3)
        + _closing_fill_fee_share(Decimal("2"), closing_quantity=2, fill_quantity=2)
    ) == Decimal("5")

    source = (ROOT / "app/execution.py").read_text(encoding="utf-8")
    assert "fill_fee_delta: Decimal | float = Decimal(\"0\")" in source
    assert "closing_fee_decimal = _closing_fill_fee_share(" in source
    assert "fill_fee_delta=fee_delta" in source
    paper_start = source.index("trade.fee = float(Decimal(str(trade.fee or 0.0)) + fee_decimal)")
    paper_apply = source.index("fill_fee_delta=fee_decimal")
    assert paper_start < paper_apply


def test_risk_sample_excludes_a_trade_when_its_fill_history_exceeds_the_cap():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    helper = source[
        source.index("async def _recent_closed_platform_trade_returns"):
        source.index('@app.get("/api/risk-dashboard/metrics")')
    ]
    assert "row_number().over(" in helper
    assert "_RISK_OUTCOME_MAX_FILL_ROWS_PER_TRADE + 1" in helper
    assert "incomplete_trade_ids = {" in helper
    assert "int(outcome.trade_id) not in incomplete_trade_ids" in helper
    assert "len(incomplete_trade_ids)" in helper


def test_legacy_withdrawal_rows_are_not_misreported_as_auth_outages():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    route = source[
        source.index('@app.get("/api/admin/withdrawals")'):
        source.index('@app.post("/api/admin/withdrawals")')
    ]
    assert "legacy_ids: set[str]" in route
    assert 'auth_email_results[auth_id] = ("", "LEGACY_IDENTITY_UNMAPPED")' in route
    assert 'email_status = "LEGACY_IDENTITY_UNMAPPED"' in route
    assert "AUTH_EMAIL_UNCONFIRMED" in source


