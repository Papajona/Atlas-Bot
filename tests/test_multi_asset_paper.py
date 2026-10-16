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
    assert point_in_time_coin_universe(rows,dt(2),coverage_start=dt(1),coverage_end=dt(4),coverage_verified=True)==("A","B")
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
