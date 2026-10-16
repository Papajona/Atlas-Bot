"""Fail-closed pure helpers for multi-asset paper simulation; not live execution."""
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Sequence
from zoneinfo import ZoneInfo

class PaperModelInputError(ValueError):
    pass

def _decimal(x, name: str) -> Decimal:
    try:
        value = Decimal(str(x))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise PaperModelInputError(f"invalid {name}") from exc
    if not value.is_finite():
        raise PaperModelInputError(f"invalid {name}")
    return value


def _pos(x, name, zero=False):
    value = _decimal(x, name)
    if value < 0 if zero else value <= 0:
        raise PaperModelInputError(f"invalid {name}")
    return value


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PaperModelInputError(f"{name} must be timezone-aware")
    return value

@dataclass(frozen=True)
class CoinListing:
    symbol: str
    listed_at: datetime
    delisted_at: datetime | None = None
    failed_at: datetime | None = None

def point_in_time_coin_universe(rows: Sequence[CoinListing], at: datetime, *, coverage_start: datetime, coverage_end: datetime, coverage_verified: bool) -> tuple[str,...]:
    if not coverage_verified: raise PaperModelInputError("historical universe coverage unverified")
    if any(x.tzinfo is None for x in (at,coverage_start,coverage_end)) or not coverage_start <= at <= coverage_end or coverage_start >= coverage_end: raise PaperModelInputError("invalid coverage window")
    seen=set()
    for r in rows:
        if not r.symbol.strip() or r.listed_at.tzinfo is None or (r.delisted_at and r.delisted_at.tzinfo is None) or (r.failed_at and r.failed_at.tzinfo is None): raise PaperModelInputError("invalid listing record")
        if r.symbol in seen: raise PaperModelInputError("duplicate symbol")
        seen.add(r.symbol)
    return tuple(sorted(r.symbol for r in rows if r.listed_at <= at and (r.delisted_at is None or at < r.delisted_at) and (r.failed_at is None or at < r.failed_at)))

@dataclass(frozen=True)
class SlippageInputs:
    mid_price: Decimal
    half_spread_bps: Decimal
    volatility_bps: Decimal
    order_quantity: Decimal
    visible_depth_quantity: Decimal
    impact_coefficient: Decimal = Decimal("1")
    adverse_gap_bps: Decimal = Decimal("0")

def estimate_execution_price(x: SlippageInputs, *, side: str, volatility_horizon_fraction: Decimal=Decimal("1")) -> tuple[Decimal,Decimal]:
    mid=_pos(x.mid_price,"mid"); qty=_pos(x.order_quantity,"quantity"); depth=_pos(x.visible_depth_quantity,"depth")
    spread=_pos(x.half_spread_bps,"spread",True); vol=_pos(x.volatility_bps,"volatility",True); coeff=_pos(x.impact_coefficient,"impact coefficient",True); gap=_pos(x.adverse_gap_bps,"gap",True); horizon=_pos(volatility_horizon_fraction,"horizon",True)
    if side not in ("buy","sell"): raise PaperModelInputError("side must be buy or sell")
    impact=coeff*vol*horizon.sqrt()*min(Decimal(1),(qty/depth).sqrt())
    bps=spread+impact+gap
    price=mid*(1+bps/Decimal(10000)*(1 if side=="buy" else -1))
    if price<=0: raise PaperModelInputError("non-positive execution price")
    return price,bps

@dataclass(frozen=True)
class FillPlan:
    requested_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    fillable: bool

def plan_partial_fill(requested_quantity: Decimal, bar_volume: Decimal, participation_rate: Decimal, *, eligible_at: datetime, bar_at: datetime, minimum_fill: Decimal=Decimal("0")) -> FillPlan:
    requested=_pos(requested_quantity,"requested"); volume=_pos(bar_volume,"volume",True); rate=_pos(participation_rate,"participation"); minimum=_pos(minimum_fill,"minimum",True)
    if rate>1 or eligible_at.tzinfo is None or bar_at.tzinfo is None: raise PaperModelInputError("invalid participation or timestamps")
    if bar_at<eligible_at or volume==0: return FillPlan(requested,Decimal(0),requested,False)
    filled=min(requested,volume*rate)
    if filled<minimum: filled=Decimal(0)
    return FillPlan(requested,filled,requested-filled,filled>0)

def stop_loss_gap_fill(*, stop_price: Decimal, bar_open: Decimal, side: str) -> Decimal:
    stop=_pos(stop_price,"stop"); opening=_pos(bar_open,"open")
    if side=="long": return min(stop,opening)
    if side=="short": return max(stop,opening)
    raise PaperModelInputError("side must be long or short")

@dataclass(frozen=True)
class SessionCalendar:
    timezone_name: str
    weekdays: frozenset[int]
    open_time: time
    close_time: time
    holidays: frozenset[date]=frozenset()
    def is_open(self, instant: datetime) -> bool:
        if instant.tzinfo is None: raise PaperModelInputError("calendar timestamp must be aware")
        local=instant.astimezone(ZoneInfo(self.timezone_name)); t=local.time().replace(tzinfo=None)
        if local.date() in self.holidays or local.weekday() not in self.weekdays: return False
        return self.open_time<=t<self.close_time if self.open_time<=self.close_time else (t>=self.open_time or t<self.close_time)

@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    contract_size: Decimal
    tick_size: Decimal
    tick_value: Decimal
    initial_margin_rate: Decimal
    maintenance_margin_rate: Decimal

def required_margin(quantity: Decimal, price: Decimal, spec: ContractSpec, *, leverage: Decimal=Decimal("1")) -> Decimal:
    qty=_pos(quantity,"quantity"); px=_pos(price,"price"); size=_pos(spec.contract_size,"contract size"); rate=_pos(spec.initial_margin_rate,"margin rate",True); lev=_pos(leverage,"leverage")
    if rate>1: raise PaperModelInputError("margin rate must be fraction <= 1")
    return qty*size*px*rate/lev

def carry_cost(*, notional: Decimal, annual_rate: Decimal, calendar_days: int, day_count_basis: int=360, direction: str="long") -> Decimal:
    n=_pos(notional,"notional"); rate=Decimal(annual_rate)
    if not rate.is_finite() or calendar_days<0 or day_count_basis<=0 or direction not in ("long","short"): raise PaperModelInputError("invalid carry inputs")
    return n*rate*Decimal(calendar_days)/Decimal(day_count_basis)*(1 if direction=="long" else -1)

def active_contract(contracts: Sequence[tuple[datetime,str]], at: datetime) -> str:
    if at.tzinfo is None or not contracts or any(t.tzinfo is None for t,_ in contracts): raise PaperModelInputError("aware timestamps and explicit rollover schedule required")
    ordered=sorted(contracts,key=lambda x:x[0])
    if len({t for t,_ in ordered})!=len(ordered) or any(not s for _,s in ordered): raise PaperModelInputError("invalid rollover schedule")
    eligible=[s for t,s in ordered if t<=at]
    if not eligible: raise PaperModelInputError("no effective contract")
    return eligible[-1]
