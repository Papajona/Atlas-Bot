import asyncio
import json
import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import app.execution as execution
import app.main as main


ROOT = Path(__file__).resolve().parents[1]


def test_customer_profile_backfill_uses_auth_claims_without_name_only_network_calls():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    block = source[source.index("async def get_customer("):source.index("def _funding_signature_valid")]
    auth_helper = source[source.index("async def _customer_auth_pii"):source.index("async def get_customer(")]
    assert "_customer_auth_pii(uid, authorization)" in block
    assert "_supabase_user(_bearer_token(authorization))" in auth_helper
    assert 'confirmed = bool(user.get("email_confirmed_at"))' in auth_helper
    assert "if confirmed else \"\"" in auth_helper
    assert "_CUSTOMER_AUTH_PII_CACHE_TTL_SECONDS = 60.0" in source
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
    assert source.count("_recent_closed_platform_trade_returns(db)") == 3
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
    auth_helper = source[source.index("async def _customer_auth_pii"):source.index("async def get_customer(")]
    assert 'confirmed = bool(user.get("email_confirmed_at"))' in auth_helper
    assert 'logger.info("customer_auth_pii_backfill_unconfirmed' in block
    assert "auth_email, auth_name, confirmed = await _customer_auth_pii(uid, authorization)" in block


def test_same_direction_position_additions_have_a_defined_zero_close_quantity():
    source = (ROOT / "app/execution.py").read_text(encoding="utf-8")
    block = source[
        source.index("async def _apply_fill_to_position"):
        source.index("async def _release_unneeded_order_reserve")
    ]
    assert block.index("closing = 0.0") < block.index("if not position:")
    assert "if closing_decimal > 0:" in block


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




def test_customer_identity_migration_always_checks_the_strategy_outcome_index():
    migration = (ROOT / "alembic/versions/0060_customer_identity_unmapped_index.py").read_text(encoding="utf-8")
    assert "if name in indexes:\n        return" not in migration
    assert "if name not in indexes:" in migration
    assert migration.index("if name not in indexes:") < migration.index("outcome_indexes =")
    assert "ix_strategy_outcome_platform_closed_trade_time" in migration


def test_partial_closes_are_aggregated_into_one_risk_observation():
    import json

    first = SimpleNamespace(
        id=1, sequence=1, lifecycle_status="OPEN",
        realized_pnl=Decimal("1.00"), net_pnl=Decimal("0.80"), fee=Decimal("0.20"),
        net_return_bps=80.0,
        metadata_json=json.dumps({"closing_notional": "100"}),
    )
    final = SimpleNamespace(
        id=2, sequence=2, lifecycle_status="OPEN",
        realized_pnl=Decimal("-1.00"), net_pnl=Decimal("-1.20"), fee=Decimal("0.20"),
        net_return_bps=-120.0,
        metadata_json=json.dumps({"closing_notional": "100"}),
    )

    summary = execution._aggregate_completed_strategy_outcomes([first, final])

    assert first.lifecycle_status == "PARTIAL_CLOSED"
    assert final.lifecycle_status == "CLOSED"
    assert summary is final
    assert final.realized_pnl == Decimal("0.00")
    assert final.net_pnl == Decimal("-0.40")
    assert final.fee == Decimal("0.40")
    assert final.net_return_bps == -20.0
    metadata = json.loads(final.metadata_json)
    assert metadata["aggregate_fill_count"] == 2
    assert metadata["aggregate_notional_complete"] is True


def test_completed_trade_risk_window_uses_aggregate_rows_not_partial_fills():
    import json

    summary = SimpleNamespace(
        id=2, trade_id=10, lifecycle_status="CLOSED",
        created_at=None, net_return_bps=-20.0,
        metadata_json=json.dumps({
            "aggregate_notional_complete": True,
            "aggregate_net_return_bps": "-20.0",
        }),
    )
    partial = SimpleNamespace(
        id=1, trade_id=10, lifecycle_status="PARTIAL_CLOSED",
        created_at=None, net_return_bps=-1000.0,
        metadata_json=json.dumps({"closing_notional": "100"}),
    )

    class Scalars:
        def all(self):
            return [summary, partial]

    class Result:
        def scalars(self):
            return Scalars()

    class Database:
        async def execute(self, _statement):
            return Result()

    returns = asyncio.run(execution._recent_completed_trade_returns(Database(), None, 5))
    assert returns == [-20.0]


def test_fee_only_live_snapshot_true_up_updates_last_closing_outcome():
    import json

    outcome = SimpleNamespace(
        id=5, sequence=2, closing_trade_id=77,
        fee=Decimal("0.10"), net_pnl=Decimal("4.90"),
        net_return_bps=490.0, updated_at=None,
        metadata_json=json.dumps({"closing_notional": "100", "net_pnl": "4.90"}),
    )

    class Scalars:
        def all(self):
            return [outcome]

    class Result:
        def scalars(self):
            return Scalars()

    class Database:
        async def execute(self, _statement):
            return Result()

    trade = SimpleNamespace(
        id=77, filled_quantity=2.0, average_fill_price=100.0,
        requested_quantity=2.0, remaining_quantity=0.0,
    )
    applied = asyncio.run(execution._apply_fill_to_position(
        Database(), trade, 2.0, 100.0, fill_fee_delta=Decimal("2.00")
    ))

    assert applied == 0.0
    assert outcome.fee == Decimal("1.10")
    assert outcome.net_pnl == Decimal("3.90")
    assert outcome.net_return_bps == 390.0
    metadata = json.loads(outcome.metadata_json)
    assert Decimal(metadata["late_fee_true_up_total"]) == Decimal("1.00")



def test_late_fee_true_up_from_earlier_partial_close_updates_final_aggregate_once():
    partial = SimpleNamespace(
        id=1, trade_id=55, sequence=1, lifecycle_status="PARTIAL_CLOSED",
        closing_trade_id=77, fee=Decimal("0.10"), net_pnl=Decimal("4.90"),
        net_return_bps=490.0,
        metadata_json=json.dumps({"closing_notional": "100", "net_pnl": "4.90"}),
    )
    final = SimpleNamespace(
        id=2, trade_id=55, sequence=2, lifecycle_status="CLOSED",
        closing_trade_id=78, fee=Decimal("0.20"), net_pnl=Decimal("9.70"),
        net_return_bps=485.0,
        metadata_json=json.dumps({
            "closing_notional": "100",
            "aggregate_closing_notional": "200",
            "aggregate_notional_complete": True,
            "aggregate_net_pnl": "9.70",
            "aggregate_net_return_bps": "485.0",
        }),
    )

    class Scalars:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return Scalars(self.rows)

    class Database:
        calls = 0

        async def execute(self, _statement):
            self.calls += 1
            return Result([partial] if self.calls == 1 else [final])

    trade = SimpleNamespace(
        id=77, filled_quantity=2.0, average_fill_price=100.0,
    )
    applied = asyncio.run(
        execution._apply_late_closing_fee_true_up(
            Database(), trade, Decimal("2.00")
        )
    )

    # Half of the reconciled order notional belongs to this closing fill.
    assert applied == Decimal("1.00")
    assert partial.net_pnl == Decimal("3.90")
    assert partial.fee == Decimal("1.10")
    # The later completed summary receives the same delta once, so dashboard
    # and risk-gate reads of the final row cannot retain the old net return.
    assert final.net_pnl == Decimal("8.70")
    assert final.fee == Decimal("1.20")
    assert final.net_return_bps == 435.0
    metadata = json.loads(final.metadata_json)
    assert Decimal(metadata["aggregate_net_pnl"]) == Decimal("8.70")
    assert metadata["aggregate_net_return_bps"] == "435.0"
    assert Decimal(metadata["late_fee_true_up_total"]) == Decimal("1.00")



def test_aggregated_partial_close_summary_uses_aggregate_notional_once():
    close_time = datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc)
    partial = SimpleNamespace(
        id=1, trade_id=91, metadata_json=json.dumps({"closing_notional": "1000"}),
        realized_pnl="10", return_bps=100, net_pnl="10",
        fee_basis_complete=True,
        created_at=close_time - timedelta(minutes=5),
    )
    final = SimpleNamespace(
        id=2, trade_id=91,
        metadata_json=json.dumps({
            "closing_notional": "4000",
            "aggregate_closing_notional": "5000",
            "aggregate_notional_complete": True,
            "aggregate_net_pnl": "39",
            "aggregate_net_return_bps": "78",
        }),
        realized_pnl="40", return_bps=80, net_pnl="39",
        fee_basis_complete=True,
        created_at=close_time,
    )
    result = main._aggregate_closed_outcome_fills([partial, final])
    assert len(result) == 1
    assert result[0][0] == pytest.approx(0.0078)
    assert result[0][1] == close_time


def test_incomplete_aggregate_partial_close_is_excluded_from_dashboard_ratios():
    close_time = datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc)
    partial = SimpleNamespace(
        id=1, trade_id=92, metadata_json=json.dumps({"closing_notional": "1000"}),
        realized_pnl="10", return_bps=100, net_pnl="10",
        fee_basis_complete=True,
        created_at=close_time - timedelta(minutes=5),
    )
    final = SimpleNamespace(
        id=2, trade_id=92,
        metadata_json=json.dumps({
            "closing_notional": "0",
            "aggregate_closing_notional": "0",
            "aggregate_notional_complete": False,
            "aggregate_net_pnl": "10",
        }),
        realized_pnl="10", return_bps=0, net_pnl="10",
        created_at=close_time,
    )
    assert main._aggregate_closed_outcome_fills([partial, final]) == []

def test_aggregate_trade_return_rejects_non_finite_notional_and_pnl():
    close_time = datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc)

    def final_outcome(*, notional, net_pnl):
        return SimpleNamespace(
            id=2,
            trade_id=93,
            metadata_json=json.dumps({
                "aggregate_closing_notional": notional,
                "aggregate_notional_complete": True,
            }),
            net_pnl=net_pnl,
            created_at=close_time,
        )

    # NaN/Infinity must never enter platform risk samples, even if the
    # aggregate completeness flag was mistakenly persisted as true.
    assert main._aggregate_closed_outcome_fills([
        final_outcome(notional="NaN", net_pnl="39")
    ]) == []
    assert main._aggregate_closed_outcome_fills([
        final_outcome(notional="5000", net_pnl="Infinity")
    ]) == []
