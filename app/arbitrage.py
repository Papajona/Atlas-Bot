from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class TriangleQuote:
    symbol: str
    bid: float
    ask: float
    bid_qty: float = 0.0
    ask_qty: float = 0.0


@dataclass(frozen=True)
class TriangleOpportunity:
    path: tuple[str, str, str]
    start_quote: float
    end_quote: float
    gross_edge_pct: float
    fees_pct: float
    slippage_buffer_pct: float
    safety_buffer_pct: float
    net_edge_pct: float
    executable: bool
    reason: str = ""


def _positive(x: float) -> bool:
    return x > 0 and x == x


def _cross(amount: float, price: float, side: str) -> float:
    if not _positive(price):
        return 0.0
    return amount * price if side == "SELL" else amount / price


def triangular_cycle(*args, **kwargs):
    raise ValueError("Use evaluate_triangle() with explicit asset continuity")


def evaluate_triangle(
    quotes: Mapping[str, TriangleQuote],
    legs: tuple[tuple[str, str, str, str], tuple[str, str, str, str], tuple[str, str, str, str]],
    start_quote: float,
    fee_rate: float,
    slippage_buffer_pct: float = 0.05,
    safety_buffer_pct: float = 0.05,
    start_asset: str = "USDT",
) -> TriangleOpportunity:
    """Evaluate a triangular cycle with explicit asset continuity and server-side costs.

    BUY consumes quote_asset and produces base_asset. SELL consumes base_asset and
    produces quote_asset. A cycle is executable only when every leg chains into the
    next and the final asset equals start_asset. Depth is checked conservatively when
    a quote supplies a non-zero top-of-book quantity.
    """
    path = tuple(x[0] for x in legs)
    if start_quote <= 0 or fee_rate < 0 or slippage_buffer_pct < 0 or safety_buffer_pct < 0:
        return TriangleOpportunity(path, start_quote, 0.0, 0.0, fee_rate * 300,
                                   slippage_buffer_pct, safety_buffer_pct, -100.0, False, "invalid_costs_or_capital")
    current_asset = start_asset.upper()
    amount = float(start_quote)
    for symbol, side, base_raw, quote_raw in legs:
        base, quote = base_raw.upper(), quote_raw.upper()
        if side not in {"BUY", "SELL"}:
            return TriangleOpportunity(path, start_quote, 0.0, 0.0, fee_rate * 300,
                                       slippage_buffer_pct, safety_buffer_pct, -100.0, False, "invalid_side")
        expected_input = quote if side == "BUY" else base
        output_asset = base if side == "BUY" else quote
        if current_asset != expected_input:
            return TriangleOpportunity(path, start_quote, 0.0, 0.0, fee_rate * 300,
                                       slippage_buffer_pct, safety_buffer_pct, -100.0, False, "asset_chain_mismatch")
        q = quotes.get(symbol)
        if q is None:
            return TriangleOpportunity(path, start_quote, 0.0, 0.0, fee_rate * 300,
                                       slippage_buffer_pct, safety_buffer_pct, -100.0, False, "missing_quote")
        px = q.ask if side == "BUY" else q.bid
        top_qty = q.ask_qty if side == "BUY" else q.bid_qty
        if not _positive(px):
            return TriangleOpportunity(path, start_quote, 0.0, 0.0, fee_rate * 300,
                                       slippage_buffer_pct, safety_buffer_pct, -100.0, False, "invalid_price")
        required_qty = amount / px if side == "BUY" else amount
        if top_qty > 0 and required_qty > top_qty:
            return TriangleOpportunity(path, start_quote, 0.0, 0.0, fee_rate * 300,
                                       slippage_buffer_pct, safety_buffer_pct, -100.0, False, "insufficient_top_of_book_depth")
        amount = _cross(amount, px, side) * (1.0 - fee_rate)
        current_asset = output_asset
    if current_asset != start_asset.upper():
        return TriangleOpportunity(path, start_quote, 0.0, 0.0, fee_rate * 300,
                                   slippage_buffer_pct, safety_buffer_pct, -100.0, False, "cycle_does_not_close")
    gross = ((amount / start_quote) - 1.0) * 100.0
    fees_pct = fee_rate * 3.0 * 100.0
    net = gross - slippage_buffer_pct - safety_buffer_pct
    return TriangleOpportunity(path, start_quote, amount, gross, fees_pct,
                               slippage_buffer_pct, safety_buffer_pct, net, net > 0,
                               "net_edge_positive" if net > 0 else "net_edge_non_positive")


@dataclass(frozen=True)
class StakeRecommendation:
    stake: float
    floor: float
    ceiling: float
    cumulative_realized_pct: float
    rationale: str


def compounded_stake(
    base_stake: float,
    cumulative_realized_pct: float,
    *,
    absolute_cap: float,
    growth_fraction: float = 0.5,
    max_growth_multiple: float = 3.0,
    drawdown_shrink: float = 0.5,
) -> StakeRecommendation:
    """Recommend the next paper-arbitrage stake from a customer's own trailing simulated results.

    This is advisory sizing, not an order: the caller still supplies (and this function
    still bounds) the actual request. Deliberately asymmetric and conservative:

    - Growth only comes from `growth_fraction` of *cumulative* realized simulated edge
      (net_edge_pct summed across the customer's own PAPER_CANDIDATE history), never from
      a single good cycle -- one lucky triangle should not double the next stake.
    - A negative cumulative record shrinks the stake toward (never below) `base_stake *
      drawdown_shrink`, so a losing streak reduces size instead of "averaging up" into it.
    - Growth is capped at `max_growth_multiple * base_stake`, and the result is always
      clamped to `absolute_cap` (the caller's arbitrage_max_capital_usdt / account equity
      limit) regardless of how large the cumulative record is.

    `cumulative_realized_pct` is a *simulated* edge sum (see arbitrage_paper's PAPER_CANDIDATE
    rows) -- it is never a claim about achievable live P&L.
    """
    if base_stake <= 0 or absolute_cap <= 0:
        return StakeRecommendation(0.0, 0.0, 0.0, cumulative_realized_pct, "invalid_base_or_cap")
    floor = base_stake * min(1.0, max(0.0, drawdown_shrink))
    ceiling = min(absolute_cap, base_stake * max(1.0, max_growth_multiple))
    if cumulative_realized_pct >= 0:
        # cumulative_realized_pct is a sum of per-cycle net_edge_pct values (percent units);
        # dividing by 100 turns that into a fraction of base_stake added as growth.
        stake = base_stake * (1.0 + max(0.0, growth_fraction) * (cumulative_realized_pct / 100.0))
        rationale = "growth_from_cumulative_simulated_edge" if stake > base_stake else "flat_no_net_edge_yet"
    else:
        severity = min(1.0, abs(cumulative_realized_pct) / 100.0)
        stake = base_stake - (base_stake - floor) * severity
        rationale = "shrink_after_simulated_losses"
    stake = max(floor, min(ceiling, stake))
    return StakeRecommendation(round(stake, 2), round(floor, 2), round(ceiling, 2),
                               round(cumulative_realized_pct, 4), rationale)
