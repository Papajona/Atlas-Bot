from datetime import datetime, timezone, time
from decimal import Decimal as D
import pytest
from app.multi_asset_paper import (
    CoinListing, PaperModelInputError, SlippageInputs, active_contract, carry_cost,
    estimate_execution_price, plan_partial_fill, point_in_time_coin_universe,
    required_margin, ContractSpec, stop_loss_gap_fill, SessionCalendar,
)

UTC=timezone.utc
def dt(day): return datetime(2025,1,day,tzinfo=UTC)

def test_point_in_time_universe_excludes_not_yet_listed_delisted_and_failed():
    rows=[CoinListing("A",dt(1)),CoinListing("B",dt(1),dt(3)),CoinListing("C",dt(1),failed_at=dt(2)),CoinListing("D",dt(3))]
    assert point_in_time_coin_universe(rows,dt(2),coverage_start=dt(1),coverage_end=dt(4),coverage_verified=True,coverage_evidence_id="sha256:universe-manifest")==("A","B")
    with pytest.raises(PaperModelInputError):
        point_in_time_coin_universe(rows,dt(2),coverage_start=dt(1),coverage_end=dt(4),coverage_verified=False)

def test_slippage_increases_with_size_volatility_and_gap():
    base=SlippageInputs(D("100"),D("2"),D("10"),D("1"),D("100"))
    _, low=estimate_execution_price(base,side="buy")
    _, high=estimate_execution_price(SlippageInputs(D("100"),D("2"),D("30"),D("80"),D("100"),adverse_gap_bps=D("5")),side="buy")
    assert high>low

def test_partial_fill_delay_and_participation_cap():
    plan=plan_partial_fill(D("10"),D("20"),D("0.1"),eligible_at=dt(2),bar_at=dt(1))
    assert plan.filled_quantity==0 and plan.remaining_quantity==10
    plan=plan_partial_fill(D("10"),D("20"),D("0.1"),eligible_at=dt(1),bar_at=dt(2))
    assert plan.filled_quantity==D("2") and plan.remaining_quantity==D("8")

def test_stop_loss_models_adverse_opening_gap():
    assert stop_loss_gap_fill(stop_price=D("95"),bar_open=D("88"),side="long")==D("88")
    assert stop_loss_gap_fill(stop_price=D("105"),bar_open=D("112"),side="short")==D("112")

def test_margin_carry_and_explicit_rollover_schedule():
    spec=ContractSpec("XAU",D("100"),D("0.01"),D("1"),D("0.1"),D("0.05"))
    assert required_margin(D("2"),D("2000"),spec)==D("400000")
    assert required_margin(D("2"),D("2000"),spec,leverage=D("10"))==D("40000.0")
    assert carry_cost(notional=D("3600"),annual_rate=D("0.05"),calendar_days=3)==D("1.5")
    assert active_contract([(dt(1),"F1"),(dt(3),"F2")],dt(3))=="F2"
    with pytest.raises(PaperModelInputError): active_contract([(dt(3),"F2")],dt(2))

def test_overnight_calendar_attributes_after_midnight_to_session_start_day():
    from datetime import date
    calendar = SessionCalendar(
        timezone_name="UTC",
        weekdays=frozenset({0}),
        open_time=time(22, 0),
        close_time=time(2, 0),
        holidays=frozenset({date(2025, 1, 7)}),
    )
    assert calendar.is_open(datetime(2025, 1, 6, 23, 0, tzinfo=UTC))
    assert calendar.is_open(datetime(2025, 1, 7, 1, 0, tzinfo=UTC))
    assert not calendar.is_open(datetime(2025, 1, 7, 23, 0, tzinfo=UTC))
    assert not calendar.is_open(datetime(2025, 1, 8, 1, 0, tzinfo=UTC))


def test_listing_dates_and_numeric_inputs_fail_closed():
    rows = [CoinListing("BAD", dt(3), delisted_at=dt(2))]
    with pytest.raises(PaperModelInputError, match="delisting precedes listing"):
        point_in_time_coin_universe(
            rows, dt(2), coverage_start=dt(1), coverage_end=dt(4), coverage_verified=True
        )
    with pytest.raises(PaperModelInputError, match="invalid mid"):
        estimate_execution_price(
            SlippageInputs("not-a-number", D("1"), D("1"), D("1"), D("1")), side="buy"
        )


def test_fill_and_carry_reject_invalid_timestamps_and_day_counts():
    with pytest.raises(PaperModelInputError, match="eligible_at must be timezone-aware"):
        plan_partial_fill(
            D("1"), D("1"), D("0.5"),
            eligible_at=datetime(2025, 1, 1), bar_at=dt(2)
        )
    with pytest.raises(PaperModelInputError, match="invalid annual rate"):
        carry_cost(notional=D("100"), annual_rate="bad", calendar_days=1)
    with pytest.raises(PaperModelInputError, match="invalid carry inputs"):
        carry_cost(notional=D("100"), annual_rate=D("0.1"), calendar_days=1.5)



def test_randomized_paper_events_are_deterministic_and_event_scoped():
    from app.multi_asset_paper import deterministic_uniform, deterministic_delay_bars
    first = deterministic_uniform(order_key="order-123", event="queue")
    assert first == deterministic_uniform(order_key="order-123", event="queue")
    assert first != deterministic_uniform(order_key="order-123", event="rejection")
    assert deterministic_delay_bars(order_key="order-123", minimum=1, maximum=4) == deterministic_delay_bars(
        order_key="order-123", minimum=1, maximum=4
    )
    assert 1 <= deterministic_delay_bars(order_key="order-123", minimum=1, maximum=4) <= 4
    with pytest.raises(PaperModelInputError, match="invalid delay bounds"):
        deterministic_delay_bars(order_key="order-123", minimum=4, maximum=1)


def test_financing_requires_verified_effective_instrument_rate_and_audit_provenance():
    from app.multi_asset_paper import VerifiedFinancingRate, verified_financing_cost
    rate = VerifiedFinancingRate(
        instrument="XAU_USD",
        long_annual_rate=D("0.05"),
        short_annual_rate=D("0.02"),
        effective_at=dt(1),
        verified_at=dt(1),
        source="broker rate statement",
        evidence_id="sha256:example-evidence-digest",
        day_count_basis=360,
        verified=True,
    )
    assert verified_financing_cost(
        notional=D("3600"), rate=rate, instrument="XAU_USD",
        at=dt(2), calendar_days=3, direction="long"
    ) == D("1.5")
    assert verified_financing_cost(
        notional=D("3600"), rate=rate, instrument="XAU_USD",
        at=dt(2), calendar_days=3, direction="short"
    ) == D("0.6")
    with pytest.raises(PaperModelInputError, match="not verified"):
        verified_financing_cost(
            notional=D("3600"), rate=VerifiedFinancingRate(
                instrument="XAU_USD", long_annual_rate=D("0.05"), short_annual_rate=D("0.02"),
                effective_at=dt(1), verified_at=dt(1), source="placeholder",
                evidence_id="placeholder", verified=False,
            ), instrument="XAU_USD", at=dt(2), calendar_days=3, direction="long"
        )
    future_verified = VerifiedFinancingRate(
        instrument="XAU_USD", long_annual_rate=D("0.05"), short_annual_rate=D("0.02"),
        effective_at=dt(1), verified_at=dt(3), source="broker schedule",
        evidence_id="sha256:future-verification", verified=True,
    )
    with pytest.raises(PaperModelInputError, match="verified after evaluation timestamp"):
        verified_financing_cost(
            notional=D("3600"), rate=future_verified, instrument="XAU_USD",
            at=dt(2), calendar_days=3, direction="long",
        )
    with pytest.raises(PaperModelInputError, match="instrument mismatch"):
        verified_financing_cost(
            notional=D("3600"), rate=rate, instrument="EUR_USD",
            at=dt(2), calendar_days=3, direction="long"
        )



def test_delisting_uses_evidenced_last_trade_or_settlement_not_forced_zero():
    from app.multi_asset_paper import resolve_delisting_mark
    mark = resolve_delisting_mark(
        last_trade_price=D("12.50"), last_trade_at=dt(2), delisting_at=dt(3),
        evidence_id="sha256:last-trade",
    )
    assert mark.price == D("12.50")
    assert mark.method == "LAST_TRADE_PROXY_NOT_REALIZED_CASH"
    settled = resolve_delisting_mark(
        last_trade_price=D("12.50"), last_trade_at=dt(2), delisting_at=dt(3),
        evidence_id="sha256:last-trade", settlement_price=D("8.25"),
        settlement_evidence_id="sha256:settlement",
    )
    assert settled.price == D("8.25")
    assert settled.method == "VERIFIED_SETTLEMENT"
    with pytest.raises(PaperModelInputError, match="settlement evidence"):
        resolve_delisting_mark(
            last_trade_price=D("12.50"), last_trade_at=dt(2), delisting_at=dt(3),
            evidence_id="sha256:last-trade", settlement_price=D("8.25"),
        )
    with pytest.raises(PaperModelInputError, match="after delisting"):
        resolve_delisting_mark(
            last_trade_price=D("12.50"), last_trade_at=dt(4), delisting_at=dt(3),
            evidence_id="sha256:last-trade",
        )


def test_new_york_rollover_is_dst_aware_and_requires_aware_timestamps():
    from app.multi_asset_paper import is_ny_rollover_window
    # 17:00 New York is 22:00 UTC in winter and 21:00 UTC in summer.
    assert is_ny_rollover_window(datetime(2025, 1, 15, 22, 0, tzinfo=UTC))
    assert is_ny_rollover_window(datetime(2025, 7, 15, 21, 0, tzinfo=UTC))
    assert not is_ny_rollover_window(datetime(2025, 7, 15, 22, 0, tzinfo=UTC))
    with pytest.raises(PaperModelInputError, match="timezone-aware"):
        is_ny_rollover_window(datetime(2025, 7, 15, 21, 0))


def test_triple_financing_weekday_must_be_explicit_and_cost_profile_is_evidence_gated():
    from app.multi_asset_paper import financing_calendar_days, InstrumentCostProfile
    from datetime import date
    # 2025-01-08 is Wednesday. No default triple day is assumed.
    assert financing_calendar_days(trade_date=date(2025, 1, 8), calendar_days=1,
                                   triple_day_weekday=None) == 1
    assert financing_calendar_days(trade_date=date(2025, 1, 8), calendar_days=1,
                                   triple_day_weekday=2) == 3
    profile = InstrumentCostProfile(
        instrument="XAU_USD", venue="OANDA", market_type="commodity",
        half_spread_bps=D("1.2"), taker_fee_bps=D("0"),
        impact_coefficient=D("0.5"), max_impact_bps=D("50"),
        effective_at=dt(1), source="verified broker schedule",
        evidence_id="sha256:cost-schedule", verified=True,
    )
    assert profile.validate(instrument="XAU_USD", venue="OANDA", at=dt(2)) is profile
    with pytest.raises(PaperModelInputError, match="not verified"):
        InstrumentCostProfile(
            instrument="XAU_USD", venue="OANDA", market_type="commodity",
            half_spread_bps=D("1.2"), taker_fee_bps=D("0"),
            impact_coefficient=D("0.5"), max_impact_bps=D("50"),
            effective_at=dt(1), source="placeholder",
            evidence_id="placeholder", verified=False,
        ).validate(instrument="XAU_USD", venue="OANDA", at=dt(2))


def test_unsupported_training_asset_does_not_fall_back_to_crypto_profile():
    import pandas as pd
    from app.trading_core import train_model
    with pytest.raises(ValueError, match="No validated training cost/annualization profile"):
        train_model(pd.DataFrame(), "unused-model-path", asset="commodity")
