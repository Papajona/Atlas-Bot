"""Fail-closed pure helpers for multi-asset paper simulation; not live execution."""
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Sequence
from zoneinfo import ZoneInfo

class PaperModelInputError(ValueError):
    pass


def deterministic_uniform(*, order_key: str, event: str, seed: str = "atlas-paper-v1") -> Decimal:
    """Stable pseudo-random value in [0, 1) for reproducible paper events.

    Never use this for security, live order routing, or cryptographic decisions.
    Distinct event labels make queue/rejection/latency draws independent by design.
    """
    import hashlib
    if not isinstance(order_key, str) or not order_key.strip():
        raise PaperModelInputError("order_key is required")
    if not isinstance(event, str) or not event.strip():
        raise PaperModelInputError("event is required")
    if not isinstance(seed, str) or not seed:
        raise PaperModelInputError("seed is required")
    digest = hashlib.sha256(f"{seed}|{order_key}|{event}".encode("utf-8")).digest()
    integer = int.from_bytes(digest[:8], "big")
    return Decimal(integer) / Decimal(2**64)


def deterministic_delay_bars(*, order_key: str, minimum: int, maximum: int, seed: str = "atlas-paper-v1") -> int:
    """Choose an inclusive integer delay reproducibly for the given order key."""
    if (not isinstance(minimum, int) or isinstance(minimum, bool)
            or not isinstance(maximum, int) or isinstance(maximum, bool)
            or minimum < 0 or maximum < minimum):
        raise PaperModelInputError("invalid delay bounds")
    span = maximum - minimum + 1
    u = deterministic_uniform(order_key=order_key, event="delay", seed=seed)
    return minimum + min(span - 1, int(u * span))


@dataclass(frozen=True)
class VerifiedFinancingRate:
    """Instrument-specific financing rate with explicit provenance and effective time.

    Rates are fractions per annum, not percentages. Placeholder/unverified rates
    are rejected so synthetic defaults cannot masquerade as broker terms.
    """
    instrument: str
    long_annual_rate: Decimal
    short_annual_rate: Decimal
    effective_at: datetime
    verified_at: datetime
    source: str
    evidence_id: str
    day_count_basis: int = 360
    verified: bool = False

    def validate(self, *, instrument: str, at: datetime) -> "VerifiedFinancingRate":
        _aware(at, "financing timestamp")
        _aware(self.effective_at, "rate effective_at")
        _aware(self.verified_at, "rate verified_at")
        if not self.verified:
            raise PaperModelInputError("financing rate is not verified")
        if self.instrument != instrument:
            raise PaperModelInputError("financing instrument mismatch")
        if self.effective_at > at:
            raise PaperModelInputError("financing rate not yet effective")
        if not self.source.strip() or not self.evidence_id.strip():
            raise PaperModelInputError("financing rate provenance is required")
        if not isinstance(self.day_count_basis, int) or isinstance(self.day_count_basis, bool) or self.day_count_basis <= 0:
            raise PaperModelInputError("invalid financing day-count basis")
        _decimal(self.long_annual_rate, "long annual rate")
        _decimal(self.short_annual_rate, "short annual rate")
        return self


def verified_financing_cost(*, notional: Decimal, rate: VerifiedFinancingRate, instrument: str,
                            at: datetime, calendar_days: int, direction: str) -> Decimal:
    """Calculate financing only from an effective, evidence-backed instrument rate."""
    rate.validate(instrument=instrument, at=at)
    if direction not in ("long", "short"):
        raise PaperModelInputError("invalid financing direction")
    annual_rate = rate.long_annual_rate if direction == "long" else rate.short_annual_rate
    # Broker financing rates are signed: positive means charged, negative means
    # credited. Do not invert short-side financing merely because the position is short.
    return carry_cost(notional=notional, annual_rate=annual_rate, calendar_days=calendar_days,
                      day_count_basis=rate.day_count_basis, direction="long")

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
    at = _aware(at, "evaluation timestamp")
    coverage_start = _aware(coverage_start, "coverage start")
    coverage_end = _aware(coverage_end, "coverage end")
    if coverage_start >= coverage_end or not coverage_start <= at <= coverage_end:
        raise PaperModelInputError("invalid coverage window")
    seen=set()
    for r in rows:
        if not isinstance(r.symbol, str) or not r.symbol.strip():
            raise PaperModelInputError("invalid listing symbol")
        _aware(r.listed_at, "listed_at")
        if r.delisted_at is not None:
            _aware(r.delisted_at, "delisted_at")
        if r.failed_at is not None:
            _aware(r.failed_at, "failed_at")
        if r.delisted_at is not None and r.delisted_at < r.listed_at:
            raise PaperModelInputError("delisting precedes listing")
        if r.failed_at is not None and r.failed_at < r.listed_at:
            raise PaperModelInputError("failure precedes listing")
        if r.symbol in seen: raise PaperModelInputError("duplicate symbol")
        seen.add(r.symbol)
    return tuple(sorted(r.symbol for r in rows if r.listed_at <= at and (r.delisted_at is None or at < r.delisted_at) and (r.failed_at is None or at < r.failed_at)))

@dataclass(frozen=True)
class DelistingMark:
    price: Decimal
    timestamp: datetime
    method: str
    evidence_id: str


def resolve_delisting_mark(*, last_trade_price: Decimal, last_trade_at: datetime,
                           delisting_at: datetime, evidence_id: str,
                           settlement_price: Decimal | None = None,
                           settlement_evidence_id: str | None = None) -> DelistingMark:
    """Resolve a delisting mark without inventing an automatic total-loss price.

    Prefer a documented settlement price. Otherwise use the last evidenced trade
    at or before delisting as a marked proxy, explicitly labelled as such; callers
    must still apply liquidation costs and must not represent this proxy as cash
    actually recovered from a venue.
    """
    last_price = _pos(last_trade_price, "last trade price")
    _aware(last_trade_at, "last_trade_at")
    _aware(delisting_at, "delisting_at")
    if last_trade_at > delisting_at:
        raise PaperModelInputError("last trade occurs after delisting")
    if not isinstance(evidence_id, str) or not evidence_id.strip():
        raise PaperModelInputError("last-trade evidence is required")
    if settlement_price is not None:
        settled = _pos(settlement_price, "settlement price")
        if not isinstance(settlement_evidence_id, str) or not settlement_evidence_id.strip():
            raise PaperModelInputError("settlement evidence is required")
        return DelistingMark(settled, delisting_at, "VERIFIED_SETTLEMENT", settlement_evidence_id)
    return DelistingMark(last_price, last_trade_at, "LAST_TRADE_PROXY_NOT_REALIZED_CASH", evidence_id)


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
    if rate > 1:
        raise PaperModelInputError("participation rate must be <= 1")
    _aware(eligible_at, "eligible_at")
    _aware(bar_at, "bar_at")
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
        _aware(instant, "calendar timestamp")
        try:
            local = instant.astimezone(ZoneInfo(self.timezone_name))
        except (KeyError, ValueError) as exc:
            raise PaperModelInputError("unknown calendar timezone") from exc
        local_time = local.time().replace(tzinfo=None)
        if self.open_time <= self.close_time:
            session_date = local.date()
            within_session = self.open_time <= local_time < self.close_time
        elif local_time >= self.open_time:
            session_date = local.date()
            within_session = True
        elif local_time < self.close_time:
            from datetime import timedelta
            session_date = local.date() - timedelta(days=1)
            within_session = True
        else:
            session_date = local.date()
            within_session = False
        if not within_session:
            return False
        return session_date.weekday() in self.weekdays and session_date not in self.holidays

def is_ny_rollover_window(instant: datetime, *, window_minutes: int = 5) -> bool:
    """Identify a configurable window around 17:00 America/New_York, DST-aware."""
    _aware(instant, "rollover timestamp")
    if not isinstance(window_minutes, int) or isinstance(window_minutes, bool) or not 0 <= window_minutes <= 60:
        raise PaperModelInputError("rollover window must be an integer from 0 to 60 minutes")
    local = instant.astimezone(ZoneInfo("America/New_York"))
    minute_of_day = local.hour * 60 + local.minute
    rollover_minute = 17 * 60
    delta = abs(minute_of_day - rollover_minute)
    delta = min(delta, 24 * 60 - delta)
    return delta <= window_minutes


def financing_calendar_days(*, trade_date: date, calendar_days: int,
                            triple_day_weekday: int | None,
                            triple_multiplier: int = 3) -> int:
    """Return chargeable days for a broker-documented triple-rollover weekday.

    Callers must pass the instrument-specific weekday from verified product terms;
    this helper deliberately has no assumed Wednesday default.
    """
    if (not isinstance(trade_date, date) or isinstance(trade_date, datetime)
            or not isinstance(calendar_days, int) or isinstance(calendar_days, bool)
            or calendar_days < 0 or triple_day_weekday not in (None, 0, 1, 2, 3, 4, 5, 6)
            or not isinstance(triple_multiplier, int) or isinstance(triple_multiplier, bool)
            or triple_multiplier < 1):
        raise PaperModelInputError("invalid financing calendar inputs")
    if triple_day_weekday is not None and trade_date.weekday() == triple_day_weekday:
        return calendar_days * triple_multiplier
    return calendar_days


@dataclass(frozen=True)
class InstrumentCostProfile:
    instrument: str
    venue: str
    market_type: str
    half_spread_bps: Decimal
    taker_fee_bps: Decimal
    impact_coefficient: Decimal
    max_impact_bps: Decimal
    effective_at: datetime
    source: str
    evidence_id: str
    verified: bool = False

    def validate(self, *, instrument: str, venue: str, at: datetime) -> "InstrumentCostProfile":
        _aware(at, "cost profile timestamp")
        _aware(self.effective_at, "cost profile effective_at")
        if not self.verified:
            raise PaperModelInputError("instrument cost profile is not verified")
        if self.instrument != instrument or self.venue != venue:
            raise PaperModelInputError("instrument cost profile context mismatch")
        if self.effective_at > at:
            raise PaperModelInputError("instrument cost profile not yet effective")
        if self.market_type not in ("spot", "forex", "commodity", "future"):
            raise PaperModelInputError("invalid cost profile market type")
        if not self.source.strip() or not self.evidence_id.strip():
            raise PaperModelInputError("instrument cost profile provenance required")
        for value, name in ((self.half_spread_bps, "half spread"),
                            (self.taker_fee_bps, "taker fee"),
                            (self.impact_coefficient, "impact coefficient"),
                            (self.max_impact_bps, "max impact")):
            _pos(value, name, True)
        return self


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    contract_size: Decimal
    tick_size: Decimal
    tick_value: Decimal
    initial_margin_rate: Decimal
    maintenance_margin_rate: Decimal

def required_margin(quantity: Decimal, price: Decimal, spec: ContractSpec, *, leverage: Decimal=Decimal("1")) -> Decimal:
    """Conservative margin: satisfy both the broker margin rate and leverage cap."""
    qty = _pos(quantity, "quantity")
    px = _pos(price, "price")
    size = _pos(spec.contract_size, "contract size")
    rate = _pos(spec.initial_margin_rate, "margin rate", True)
    lev = _pos(leverage, "leverage")
    if rate > 1:
        raise PaperModelInputError("margin rate must be a fraction <= 1")
    effective_rate = max(rate, Decimal("1") / lev)
    return qty * size * px * effective_rate

def carry_cost(*, notional: Decimal, annual_rate: Decimal, calendar_days: int, day_count_basis: int=360, direction: str="long") -> Decimal:
    n = _pos(notional, "notional")
    rate = _decimal(annual_rate, "annual rate")
    if (not isinstance(calendar_days, int) or isinstance(calendar_days, bool)
            or not isinstance(day_count_basis, int) or isinstance(day_count_basis, bool)
            or calendar_days < 0 or day_count_basis <= 0
            or direction not in ("long", "short")):
        raise PaperModelInputError("invalid carry inputs")
    return n * rate * Decimal(calendar_days) / Decimal(day_count_basis) * (1 if direction == "long" else -1)

def active_contract(contracts: Sequence[tuple[datetime,str]], at: datetime) -> str:
    _aware(at, "rollover timestamp")
    if not contracts:
        raise PaperModelInputError("explicit rollover schedule required")
    for timestamp, symbol in contracts:
        _aware(timestamp, "rollover effective timestamp")
        if not isinstance(symbol, str) or not symbol.strip():
            raise PaperModelInputError("invalid rollover contract symbol")
    ordered=sorted(contracts,key=lambda x:x[0])
    if len({t for t,_ in ordered})!=len(ordered) or any(not s for _,s in ordered): raise PaperModelInputError("invalid rollover schedule")
    eligible=[s for t,s in ordered if t<=at]
    if not eligible: raise PaperModelInputError("no effective contract")
    return eligible[-1]
