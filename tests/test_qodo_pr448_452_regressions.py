import asyncio
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app import execution, main


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
    assert "needs_auth_identity = profile is None or not str(profile.email or" in block
    assert "await _has_unmapped_legacy_customer_profile(db, uid)" in block
    assert "casefold()" in block


def test_legacy_email_duplicate_detection_uses_decrypted_orm_values_not_sql_like():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    conflict_block = source[
        source.index("async def _legacy_customer_email_conflict"):
        source.index("async def _has_unmapped_legacy_customer_profile")
    ]
    unmapped_block = source[
        source.index("async def _has_unmapped_legacy_customer_profile"):
        source.index("async def _customer_profile_effectively_active")
    ]
    block = conflict_block + unmapped_block
    normalized_conflict = " ".join(conflict_block.split())
    assert "select( CustomerProfile.auth_user_id, CustomerProfile.email )" in normalized_conflict
    assert "select(CustomerProfile.id)" in unmapped_block
    assert "uuid.UUID" in block
    assert 'CustomerProfile.auth_user_id.op("!~*")' in block
    assert 'func.lower(CustomerProfile.auth_user_id).op("NOT GLOB")' in block
    assert "literal_column" in block
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
    assert portfolio.index("get_existing_customer(authorization, db)") < portfolio.index("await _paper_state_guard()")
    assert portfolio.index("await _paper_state_guard()") < portfolio.index("get_customer_paper_balances")
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


def test_risk_dashboard_queries_bounded_completed_trade_summaries():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert source.count("rows = await _recent_closed_platform_outcome_returns(db)") == 2
    assert "latest_closed =" not in source
    helper = source[
        source.index("async def _recent_closed_platform_outcome_returns"):
        source.index("async def risk_dashboard_metrics")
    ]
    assert "StrategyOutcome.closing_notional > 0" in helper
    assert ".order_by(StrategyOutcome.created_at.desc())" in helper
    assert ".limit(100)" in helper
    model = (ROOT / "app/db.py").read_text(encoding="utf-8")
    assert 'Index("ix_strategy_outcome_platform_completed", "customer_id", "lifecycle_status", "created_at")' in model
    assert "closing_notional:" in model
    ratios = (ROOT / "app/risk_metrics.py").read_text(encoding="utf-8")
    assert "MIN_ANNUALIZATION_SPAN_SECONDS = 30 * 86400" in ratios
    assert "interval_count = len(times) - 1" in ratios


def test_partial_closing_fills_aggregate_to_one_complete_trade_return():
    first = SimpleNamespace(
        id=1, sequence=1, closing_notional=Decimal("100"),
        net_pnl=Decimal("1.90"), realized_pnl=Decimal("2.00"),
        fee=Decimal("0.10"), return_bps=200.0, net_return_bps=190.0,
        closing_quantity=Decimal("1"), fee_basis_complete=True,
        lifecycle_status="OPEN", metadata_json="{}",
    )
    final = SimpleNamespace(
        id=2, sequence=2, closing_notional=Decimal("300"),
        net_pnl=Decimal("5.70"), realized_pnl=Decimal("6.00"),
        fee=Decimal("0.30"), return_bps=200.0, net_return_bps=190.0,
        closing_quantity=Decimal("3"), fee_basis_complete=True,
        lifecycle_status="CLOSED", metadata_json="{}",
    )

    result = execution._aggregate_completed_strategy_outcomes([first, final])

    assert result is final
    assert first.lifecycle_status == "PARTIAL_CLOSED"
    assert final.lifecycle_status == "CLOSED"
    assert final.closing_notional == Decimal("400")
    assert final.closing_quantity == Decimal("4")
    assert final.net_pnl == Decimal("7.60")
    assert final.realized_pnl == Decimal("8.00")
    assert final.fee == Decimal("0.40")
    assert final.return_bps == 200.0
    assert final.net_return_bps == 190.0
    assert '"aggregate_fill_count": 2' in final.metadata_json
    assert '"final_fill_closing_quantity": "3"' in final.metadata_json
    assert '"final_fill_closing_notional": "300"' in final.metadata_json


def test_execution_safety_windows_use_only_completed_trade_outcomes():
    source = (ROOT / "app/execution.py").read_text(encoding="utf-8")
    health_start = source.index("async def _execution_health_block")
    health_end = source.index("\nasync def ", health_start + 1)
    health = source[health_start:health_end]
    assert 'StrategyOutcome.lifecycle_status == "CLOSED"' in health

    edge_start = source.index("qout = (")
    edge_end = source.index("realized =", edge_start)
    edge_decay = source[edge_start:edge_end]
    assert 'StrategyOutcome.lifecycle_status == "CLOSED"' in edge_decay
    assert "StrategyOutcome.closing_notional > 0" in health
    assert "StrategyOutcome.closing_notional > 0" in edge_decay
    router = (ROOT / "app/main.py").read_text(encoding="utf-8")
    adaptive = router[
        router.index("if settings.adaptive_strategy_router_enabled"):
        router.index("current_strategy =")
    ]
    assert "StrategyOutcome.closing_notional > 0" in adaptive


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
    assert 'if not str(stored_email or "").strip():\n            return True' in helper

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
    assert '"identity_review_required": bool(getattr(profile, "identity_review_required", False))' in summary

    get_customer = source[
        source.index("async def get_customer("):
        source.index("def _funding_signature_valid")
    ]
    assert "if identity_review_required and not allow_identity_review_summary" in get_customer
    assert "if profile is None:" in get_customer
    assert "identity_review_required = await _has_unmapped_legacy_customer_profile(db, uid)" in get_customer
    assert "await _legacy_customer_email_conflict(db, uid, email)" in get_customer
    assert "await _customer_profile_effectively_active(guard_db, current_profile)" in source
    assert "profile.status = \"PENDING_IDENTITY_REVIEW\"" not in get_customer
    assert "if not identity_review_required:" in get_customer
    assert "allow_identity_review_summary" in get_customer
    assert "PENDING_IDENTITY_REVIEW" in get_customer


def test_paper_customer_access_fails_closed_while_legacy_auth_references_remain():
    source = (ROOT / "app/paper_staging.py").read_text(encoding="utf-8")
    gate = source[
        source.index("async def _has_unmapped_legacy_customer_profile"):
        source.index("async def _paper_state_guard")
    ]
    assert 'op("!~*")' in gate
    assert 'op("GLOB")' in gate
    assert ".limit(1)" in gate
    assert '"has_stored_email"' in gate
    assert 'status = str(row.status or "").upper()' in gate
    assert 'identity_review_required = status == "PENDING_IDENTITY_REVIEW"' in gate
    assert 'status == "ACTIVE" and not has_stored_email' in gate
    assert "await _has_unmapped_legacy_customer_profile(db, uid)" in gate
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
    assert "if identity_review_required and not allow_identity_review_summary:" in access
    assert "identity_review_required=identity_review_required" in access
    assert "effective_status = row.status" in access
    assert "identity_review_required=identity_review_required" in access
    assert "status=effective_status" in access


def test_customer_identity_backfill_avoids_name_only_auth_and_persists_review_quarantine():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    customer = source[
        source.index("async def get_customer("):
        source.index("def _funding_signature_valid")
    ]
    assert "not str(profile.display_name or" not in customer
    assert "needs_auth_identity = profile is None or not str(profile.email or" in customer
    assert "needs_auth_identity = profile is None or not str(profile.email or \"\").strip()" in customer
    assert 'profile.status = "PENDING_IDENTITY_REVIEW"' in customer
    assert "await db.commit()" in customer
    assert "await db.refresh(profile)" in customer
    assert "profile.identity_review_required = identity_review_required" in customer
    assert "profile.status = \"ACTIVE\"" not in customer
    assert "await _has_unmapped_legacy_customer_profile(db, uid)" in customer
    assert "stale_legacy_quarantine" in customer


def test_edge_legacy_identity_offset_paging_has_a_stable_order():
    edge = (ROOT / "supabase/functions/customer-profile-provision/index.ts").read_text(encoding="utf-8")
    paging = edge[
        edge.index("const pageSize = 500"):
        edge.index("// Atlas stores customer_profiles.email")
    ]
    assert '.order("id", { ascending: true })' in paging
    assert paging.index('.order("id", { ascending: true })') < paging.index(".range(offset, offset + pageSize - 1)")


def test_outcome_aggregation_excludes_unknown_quantity_or_notional():
    unknown = SimpleNamespace(
        id=3, sequence=1, closing_quantity=Decimal("0"),
        closing_notional=Decimal("0"), net_pnl=Decimal("0"),
        realized_pnl=Decimal("0"), fee=Decimal("0"),
        return_bps=0.0, net_return_bps=0.0,
        lifecycle_status="OPEN", metadata_json="{}",
    )
    result = execution._aggregate_completed_strategy_outcomes([unknown])
    assert result is unknown
    assert result.lifecycle_status == "CLOSED"
    assert result.closing_quantity == Decimal("0")
    assert result.closing_notional == Decimal("0")
    assert result.net_return_bps is None


def test_revision_0060_is_batched_online_only_and_refuses_a_false_downgrade():
    migration = (
        ROOT / "alembic/versions/0061_strategy_outcome_fill_aggregation.py"
    ).read_text(encoding="utf-8")
    assert "context.is_offline_mode()" in migration
    assert "SELECT DISTINCT trade_id" in migration
    assert "_BATCH_SIZE = 100" in migration
    assert "lifecycle_status IN ('OPEN', 'CLOSED')" in migration
    assert "return_bps" in migration and "realized_pnl" in migration
    assert "filled_quantity" in migration and "average_fill_price" in migration
    assert ".mappings().all()" in migration
    assert "raise RuntimeError(" in migration
    assert '"closing_quantity",' in migration
    assert '"entry_fee",' in migration


def test_fill_outcomes_use_per_fill_fees_and_reversal_close_only_quantity():
    source = (ROOT / "app/execution.py").read_text(encoding="utf-8")
    function = source[
        source.index("async def _apply_fill_to_position"):
        source.index("async def _release_unneeded_order_reserve")
    ]
    assert "current_fill_fee" in function
    assert "current_fee_decimal" in function
    assert "closing_order_fee_decimal" in function
    assert "closing_quantity=closing_decimal" in function
    assert "position_flipped or abs(float(position.quantity or 0.0)) < 1e-12" in function
    assert "current_fee_decimal = Decimal(str(current_fill_fee or 0))" in function
    assert "closing_fee_decimal = closing_order_fee_decimal" in function
    assert "fee_for_completed_trade = closing_fee_decimal + allocated_entry_fee_decimal" in function
    assert "for outcome in completed_outcomes:" not in function



def test_strategy_outcome_aggregates_only_current_lifecycle_and_carries_fee_completeness():
    partial = SimpleNamespace(
        id=10, sequence=2, lifecycle_status="OPEN",
        closing_notional=Decimal("100"), closing_quantity=Decimal("1"),
        net_pnl=Decimal("0.8"), realized_pnl=Decimal("1"), fee=Decimal("0.2"),
        fee_basis_complete=True, metadata_json="{}",
    )
    final = SimpleNamespace(
        id=11, sequence=3, lifecycle_status="CLOSED",
        closing_notional=Decimal("100"), closing_quantity=Decimal("1"),
        net_pnl=Decimal("-1.2"), realized_pnl=Decimal("-1"), fee=Decimal("0.2"),
        fee_basis_complete=False, metadata_json="{}",
    )
    old_lifecycle = SimpleNamespace(
        id=9, sequence=1, lifecycle_status="CLOSED",
        closing_notional=Decimal("500"), closing_quantity=Decimal("5"),
        net_pnl=Decimal("4"), realized_pnl=Decimal("5"), fee=Decimal("1"),
        fee_basis_complete=True, metadata_json="{}",
    )

    aggregate = execution._aggregate_completed_strategy_outcomes([partial, final])
    assert aggregate is final
    assert final.fee_basis_complete is False
    assert final.net_pnl is None
    assert final.net_return_bps is None
    metadata = json.loads(final.metadata_json)
    assert metadata["aggregate_fee_basis_complete"] is False
    assert metadata["net_pnl"] is None
    assert old_lifecycle.lifecycle_status == "CLOSED"



def _load_strategy_outcome_migration():
    import importlib.util

    path = ROOT / "alembic/versions/0061_strategy_outcome_fill_aggregation.py"
    spec = importlib.util.spec_from_file_location("strategy_outcome_0061", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_historical_fee_reconstruction_counts_each_source_order_once():
    migration = _load_strategy_outcome_migration()
    source_trades = {
        10: {"id": 10, "fee": "2.25", "mode": "PAPER"},
        20: {"id": 20, "fee": "1.50", "mode": "PAPER"},
        30: {"id": 30, "fee": "0.75", "mode": "PAPER"},
    }
    assert migration._reconstruct_trade_fee_total(
        10, [20, 20, 30], source_trades
    ) == Decimal("4.50")
    live_sources = {
        key: {**value, "mode": "LIVE"} for key, value in source_trades.items()
    }
    assert migration._reconstruct_trade_fee_total(
        10, [20, 30], live_sources
    ) is None
    assert migration._reconstruct_trade_fee_total(
        10, [20, 99], source_trades
    ) is None


def test_open_position_entry_fee_backfill_requires_full_quantity_reconciliation():
    migration = _load_strategy_outcome_migration()
    entry_trade = {"filled_quantity": "4", "fee": "8", "mode": "PAPER"}
    assert migration._reconstruct_remaining_entry_fee(
        entry_trade, Decimal("1"), Decimal("3")
    ) == Decimal("2")
    # A top-up, missing close, or incomplete history cannot silently become zero fee.
    assert migration._reconstruct_remaining_entry_fee(
        entry_trade, Decimal("2"), Decimal("1")
    ) is None
    assert migration._reconstruct_remaining_entry_fee(
        None, Decimal("1"), Decimal("3")
    ) is None



def test_exchange_fee_quality_distinguishes_reported_from_estimated_values():
    from app.live_fees import EXACT, CONVERTED, ESTIMATED, fee_quality_is_verified

    assert fee_quality_is_verified(EXACT) is True
    assert fee_quality_is_verified(CONVERTED) is True
    assert fee_quality_is_verified(ESTIMATED) is False
    assert fee_quality_is_verified(None) is False
    assert fee_quality_is_verified("unexpected") is False


def test_historical_variable_price_fill_notional_reconciles_by_quantity():
    migration = _load_strategy_outcome_migration()
    # Two fills for one order: 1 @ 100 and 2 @ 120. The recorded order's
    # cumulative notional may be 3 @ 120 = 360; actual fill notional is 340.
    # Quantity and per-fill evidence are complete despite different notionals.
    assert migration._reconcile_closing_order_fill_evidence(
        Decimal("3"), Decimal("3"), Decimal("340")
    ) is True
    assert migration._reconcile_closing_order_fill_evidence(
        Decimal("3"), Decimal("2"), Decimal("340")
    ) is False
    assert migration._reconcile_closing_order_fill_evidence(
        Decimal("3"), Decimal("3"), Decimal("0")
    ) is False


def test_metadata_declares_the_partial_indexes_created_by_revision_0060():
    from app.db import CustomerProfile, StrategyOutcome

    customer = {
        item.name: item for item in CustomerProfile.__table__.indexes
    }
    outcomes = {
        item.name: item for item in StrategyOutcome.__table__.indexes
    }
    assert "ix_customer_profiles_unmapped_auth_user_id" in customer
    assert "auth_user_id !~*" in str(
        customer["ix_customer_profiles_unmapped_auth_user_id"].dialect_options["postgresql"]["where"]
    )
    assert "NOT GLOB" in str(
        customer["ix_customer_profiles_unmapped_auth_user_id"].dialect_options["sqlite"]["where"]
    )
    assert "ix_strategy_outcome_platform_closed_trade_time" in outcomes
    predicate = str(
        outcomes["ix_strategy_outcome_platform_closed_trade_time"].dialect_options["postgresql"]["where"]
    )
    assert "customer_id IS NULL" in predicate
    assert "lifecycle_status = 'CLOSED'" in predicate



def test_missing_currency_fee_is_not_debited_or_saved_as_authoritative(monkeypatch):
    from app.live_fees import UNVERIFIED

    trade = SimpleNamespace(
        id=7701, customer_id=91, symbol="BTC/USDT", fee=Decimal("0")
    )
    delta, cumulative, quality, note = execution._prepare_live_fee(
        trade,
        {"fee": {"cost": "0.25"}},
        filled=0.1,
        fill_price=60000,
        taker_bps=5.5,
    )

    assert delta == Decimal("0")
    assert cumulative == Decimal("0.000000")
    assert quality == UNVERIFIED
    assert trade.fee == Decimal("0")
    calls = {"settle": [], "incident": []}

    async def fake_settle(*args, **kwargs):
        calls["settle"].append((args, kwargs))

    async def fake_incident(*args, **kwargs):
        calls["incident"].append((args, kwargs))

    monkeypatch.setattr(execution, "settle_trading_fee", fake_settle)
    monkeypatch.setattr(execution, "record_ledger_incident", fake_incident)

    asyncio.run(
        execution._post_live_fee(
            None, trade, delta, cumulative, quality, note
        )
    )

    assert calls["settle"] == []
    assert len(calls["incident"]) == 1
    assert calls["incident"][0][1]["detail"]["fee_quality"] == UNVERIFIED


def test_later_verified_fee_snapshot_charges_only_unsettled_authoritative_delta():
    trade = SimpleNamespace(id=7702, customer_id=92, symbol="BTC/USDT", fee=Decimal("0"))
    missing_delta, _cumulative, missing_quality, _note = execution._prepare_live_fee(
        trade, {"fee": {"cost": "0.25"}}, 0.1, 60000, taker_bps=5.5
    )
    assert missing_delta == Decimal("0")
    assert trade.fee == Decimal("0")

    delta, cumulative, quality, _note = execution._prepare_live_fee(
        trade, {"fee": {"currency": "USDT", "cost": "0.25"}},
        0.1, 60000, taker_bps=5.5
    )
    assert quality == "exact"
    assert delta == Decimal("0.250000")
    assert cumulative == Decimal("0.250000")
    assert trade.fee == Decimal("0.250000")

def test_stale_identity_quarantine_reactivates_only_after_conflict_clears(monkeypatch):
    profile = SimpleNamespace(
        status="PENDING_IDENTITY_REVIEW",
        auth_user_id="customer-auth-id",
        email="customer@example.test",
    )

    async def no_conflict(_db, _auth_user_id, _email):
        return False

    async def conflict_remains(_db, _auth_user_id, _email):
        return True

    monkeypatch.setattr(main, "_legacy_customer_email_conflict", no_conflict)
    assert asyncio.run(main._customer_profile_effectively_active(object(), profile)) is True

    monkeypatch.setattr(main, "_legacy_customer_email_conflict", conflict_remains)
    assert asyncio.run(main._customer_profile_effectively_active(object(), profile)) is False

    inactive = SimpleNamespace(status="SUSPENDED", auth_user_id="customer-auth-id", email="customer@example.test")
    assert asyncio.run(main._customer_profile_effectively_active(object(), inactive)) is False


def test_adaptive_and_live_executor_gates_use_effective_customer_identity_status():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    adaptive_start = source.index("async def _adaptive_bot_runtime_gate")
    adaptive_end = source.index("\nasync def ", adaptive_start + 1)
    adaptive = source[adaptive_start:adaptive_end]
    executor_start = source.index("async def _executor_controller_loop")
    executor_end = source.index("\nasync def ", executor_start + 1)
    executor = source[executor_start:executor_end]
    cycle_start = source.index("async def _run_persisted_adaptive_bot_cycle")
    cycle_end = source.index("\nasync def ", cycle_start + 1)
    cycle = source[cycle_start:cycle_end]
    assert "await _customer_profile_effectively_active(db, profile)" in adaptive
    assert "await _customer_profile_effectively_active(db, profile)" in executor
    assert "await _customer_profile_effectively_active(db, profile)" in cycle
    assert 'profile.status != "ACTIVE"' not in adaptive
    assert 'profile.status == "ACTIVE"' not in executor
    assert 'profile.status == "ACTIVE"' not in cycle
    assert 'bot.last_stage = "customer_identity"' in cycle
    # Customer-owned PAPER executors also recheck identity before the LIVE/PAPER split.
    assert executor.index("if cid is not None and (") < executor.index('if mode == "LIVE":')
    assert 'row.last_error = "Customer identity is pending manual review or remains conflicted"' in executor



def test_identity_conflict_is_quarantined_before_identity_summary_returns(monkeypatch):
    profile = SimpleNamespace(
        id=501,
        auth_user_id="identity-conflict-user",
        email="",
        display_name="",
        status="ACTIVE",
    )

    class Result:
        def scalar_one_or_none(self):
            return profile

    class FakeDB:
        def __init__(self):
            self.commits = 0

        async def execute(self, _statement):
            return Result()

        async def flush(self):
            return None

        async def commit(self):
            self.commits += 1

        async def refresh(self, _instance):
            return None

    db = FakeDB()

    async def customer_claims(_authorization, *, require_aal2=True):
        assert require_aal2 is True
        return {"sub": profile.auth_user_id, "email": ""}

    async def supabase_user(_token):
        return {
            "id": profile.auth_user_id,
            "email": "verified@example.invalid",
            "email_confirmed_at": "2026-11-24T00:00:00Z",
        }

    async def legacy_conflict(_db, _uid, _email):
        return True

    async def unmapped_legacy(_db, _uid):
        return True

    monkeypatch.setattr(main, "customer_claims", customer_claims)
    monkeypatch.setattr(main, "_supabase_user", supabase_user)
    monkeypatch.setattr(main, "_legacy_customer_email_conflict", legacy_conflict)
    monkeypatch.setattr(main, "_has_unmapped_legacy_customer_profile", unmapped_legacy)

    returned, _claims = asyncio.run(
        main.get_customer(
            "Bearer identity-token",
            db,
            allow_identity_review_summary=True,
        )
    )

    assert returned is profile
    assert profile.status == "PENDING_IDENTITY_REVIEW"
    assert profile.identity_review_required is True
    assert db.commits == 1
    assert asyncio.run(main._customer_profile_effectively_active(db, profile)) is False


def test_identity_conflict_is_committed_before_customer_access_is_rejected(monkeypatch):
    profile = SimpleNamespace(
        id=502,
        auth_user_id="identity-conflict-denied-user",
        email="",
        display_name="",
        status="ACTIVE",
    )

    class Result:
        def scalar_one_or_none(self):
            return profile

    class FakeDB:
        def __init__(self):
            self.commits = 0

        async def execute(self, _statement):
            return Result()

        async def flush(self):
            return None

        async def commit(self):
            self.commits += 1

        async def refresh(self, _instance):
            return None

    db = FakeDB()

    async def customer_claims(_authorization, *, require_aal2=True):
        assert require_aal2 is True
        return {"sub": profile.auth_user_id, "email": ""}

    async def supabase_user(_token):
        return {
            "id": profile.auth_user_id,
            "email": "verified@example.invalid",
            "email_confirmed_at": "2026-11-24T00:00:00Z",
        }

    async def legacy_conflict(_db, _uid, _email):
        return True

    monkeypatch.setattr(main, "customer_claims", customer_claims)
    monkeypatch.setattr(main, "_supabase_user", supabase_user)
    monkeypatch.setattr(main, "_legacy_customer_email_conflict", legacy_conflict)

    try:
        asyncio.run(main.get_customer("Bearer identity-token", db))
    except main.HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("Conflicted identity was not rejected")

    assert profile.status == "PENDING_IDENTITY_REVIEW"
    assert db.commits == 1
