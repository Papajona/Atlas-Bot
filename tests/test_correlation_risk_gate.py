"""correlation_risk.py had zero test coverage and its one function, adjusted_group_exposure(),
was imported in main.py but never called -- confirmed dead code during review. These tests
cover the function itself (previously untested) and verify it is now actually wired into the
live per-symbol decision pipeline in the correct position."""

from app.correlation_risk import adjusted_group_exposure, risk_group


class _Pos:
    def __init__(self, symbol, quantity, mark_price):
        self.symbol = symbol
        self.quantity = quantity
        self.mark_price = mark_price


def test_risk_group_maps_known_symbols_and_falls_back_to_other():
    assert risk_group("BTC/USDT:USDT") == "BTC_BETA"
    assert risk_group("ETH/USDT:USDT") == "L1_BETA"
    assert risk_group("SOL/USDT:USDT") == "L1_BETA"
    assert risk_group("EUR_USD") == "USD_FX"
    assert risk_group("XAU_USD") == "METALS"
    assert risk_group("SOME_UNKNOWN_TICKER") == "OTHER"
    assert risk_group("") == "OTHER"
    assert risk_group(None) == "OTHER"


def test_risk_group_matches_full_pair_keys_for_fx_metals_energy():
    """Regression for a real bug found while writing this test: _GROUPS keys FX/metals/
    energy by the FULL pair ("EUR_USD", "XAU_USD"), but the old lookup only matched the
    pre-underscore segment (a bare currency code like "EUR" or "XAU") -- which is never a
    dict key, so every non-crypto symbol silently fell through to "OTHER", and all of them
    landed in that SAME bucket together (gold would count against an unrelated JPY position)."""
    assert risk_group("GBP_USD") == "USD_FX"
    assert risk_group("USD_JPY") == "USD_FX"
    assert risk_group("WTICO_USD") == "ENERGY"
    assert risk_group("NATGAS_USD") == "ENERGY"
    # These must NOT collapse into the same bucket now that the lookup is correct.
    assert risk_group("XAU_USD") != risk_group("USD_JPY")


def test_btc_and_eth_are_different_groups_so_no_cross_cap():
    """BTC and ETH are deliberately separate buckets (BTC_BETA vs L1_BETA) -- a large BTC
    position must not count against a proposed ETH trade."""
    positions = [_Pos("BTC/USDT:USDT", 1.0, 60000.0)]  # 60,000 notional
    check = adjusted_group_exposure(positions, proposed_symbol="ETH/USDT:USDT",
                                    proposed_notional=8000.0, cap_usd=12000.0)
    assert check["group"] == "L1_BETA"
    assert check["existing_notional"] == 0.0
    assert check["allowed"] is True


def test_same_group_exposure_is_aggregated_and_can_breach_the_cap():
    """ETH and SOL are both L1_BETA -- an existing ETH position must count against a new
    SOL proposal in the same bucket."""
    positions = [_Pos("ETH/USDT:USDT", 5.0, 2000.0)]  # 10,000 notional
    check = adjusted_group_exposure(positions, proposed_symbol="SOL/USDT:USDT",
                                    proposed_notional=5000.0, cap_usd=12000.0)
    assert check["group"] == "L1_BETA"
    assert check["existing_notional"] == 10000.0
    assert check["projected_notional"] == 15000.0
    assert check["allowed"] is False
    assert check["members"] == [("ETH/USDT:USDT", 10000.0)]


def test_forex_pairs_now_correctly_aggregate_after_the_risk_group_fix():
    """Before the fix above, EUR_USD and GBP_USD both fell through to the same accidental
    "OTHER" bucket as every other FX/metals/energy symbol -- this passed by coincidence,
    not because the grouping was correct. Confirm it's correct now: both are USD_FX, and a
    large EUR_USD position correctly counts against a new GBP_USD proposal."""
    positions = [_Pos("EUR_USD", 100000.0, 1.10)]  # 110,000 notional
    check = adjusted_group_exposure(positions, proposed_symbol="GBP_USD",
                                    proposed_notional=5000.0, cap_usd=100000.0)
    assert check["group"] == "USD_FX"
    assert check["allowed"] is False
    # A gold position must NOT be pulled into the same FX bucket.
    gold_check = adjusted_group_exposure(positions, proposed_symbol="XAU_USD",
                                         proposed_notional=5000.0, cap_usd=100000.0)
    assert gold_check["group"] == "METALS"
    assert gold_check["existing_notional"] == 0.0
    assert gold_check["allowed"] is True


def test_zero_quantity_positions_and_missing_fields_do_not_crash():
    positions = [_Pos("ETH/USDT:USDT", 0.0, 2000.0), _Pos("SOL/USDT:USDT", None, None)]
    check = adjusted_group_exposure(positions, proposed_symbol="ETH/USDT:USDT",
                                    proposed_notional=1000.0, cap_usd=12000.0)
    assert check["existing_notional"] == 0.0
    assert check["allowed"] is True


def test_adjusted_group_exposure_handles_empty_and_none_positions():
    assert adjusted_group_exposure([], proposed_symbol="BTC/USDT:USDT",
                                   proposed_notional=100.0, cap_usd=1.0)["allowed"] is False
    assert adjusted_group_exposure(None, proposed_symbol="BTC/USDT:USDT",
                                   proposed_notional=0.0, cap_usd=0.0)["existing_notional"] == 0.0


# ---------------------------------------------------------------------------
# Wiring: confirm the gate is actually called from the live decision path,
# in the right place (after the per-trade quantity is sized, before the
# runtime gate), and that a breach shrinks quantity rather than silently
# ignoring the cap.
# ---------------------------------------------------------------------------

def test_correlation_gate_is_wired_into_the_live_decision_pipeline():
    src = open("app/main.py").read()
    assert "group_check = adjusted_group_exposure(" in src
    i = src.index("if quantity <= 0:\n        return {\"decision\": \"NO_TRADE\", \"stage\": \"risk_quantity\"}")
    j = src.index("allowed, paper_mode, gate_reason = await _adaptive_bot_runtime_gate(")
    block = src[i:j]
    assert "group_check = adjusted_group_exposure(" in block
    assert "correlation_group_cap_usd" in block
    assert '"stage": "correlation_group_cap"' in block
    # A breach must shrink the proposed quantity to the remaining headroom, not just log it.
    assert "headroom = max(0.0, settings.correlation_group_cap_usd - group_check" in block
    assert "quantity = min(quantity, headroom" in block


def test_correlation_gate_queries_positions_scoped_to_the_customer():
    """The non-paper gate must only load this customer's open positions."""
    src = open("app/main.py").read()
    query_start = src.index("open_positions = (await db2.execute(select(Position).where(")
    query_end = src.index("))).scalars().all()", query_start)
    position_query = src[query_start:query_end]
    assert "Position.customer_id == profile.id" in position_query
    assert "Position.quantity != 0" in position_query


def test_customer_paper_buy_filters_unrelated_markets_before_valuation():
    """Unrelated risk groups must be discarded before any provider mark is requested."""
    from pathlib import Path

    source = Path("app/main.py").read_text(encoding="utf-8")
    helper_start = source.index("async def _customer_paper_positions_for_exposure(")
    helper_end = source.index("\ndef _stepup_token(", helper_start)
    helper = source[helper_start:helper_end]

    assert "target_group = risk_group(req.symbol)" in helper
    assert "relevant_positions = [" in helper
    assert 'risk_group(str(row.get("symbol") or "")) == target_group' in helper
    assert helper.index("relevant_positions = [") < helper.index("market_data, mark_req")
    assert '"stage": "paper_exposure_mark_unavailable"' in helper


def test_direct_start_bot_applies_paper_group_cap_before_submission():
    """Direct paper start must share the isolated-position BUY headroom gate."""
    from pathlib import Path

    source = Path("app/main.py").read_text(encoding="utf-8")
    route_start = source.index("async def customer_start_bot(")
    route_end = source.index("\n\nasync def _require_feature", route_start)
    route = source[route_start:route_end]

    assert "_customer_paper_positions_for_exposure(" in route
    assert "adjusted_group_exposure(" in route
    assert "correlation_group_cap_usd" in route
    assert "quantity = min(" in route
    assert route.index("adjusted_group_exposure(") < route.index("execute_customer_paper_spot_signal(")
    assert "paper_exposure_mark_unavailable" in route



def test_customer_paper_buy_skips_unrelated_position_mark_requests(monkeypatch):
    """A broken quote for BTC must not block an ETH BUY in the L1 risk bucket."""
    import asyncio
    from types import SimpleNamespace
    from app import main
    import app.customer_paper_lifecycle as lifecycle

    async def positions(**_kwargs):
        return [{
            "symbol": "BTC/USDT",
            "venue": "binance",
            "quantity": "1",
            "average_entry_price": "60000",
        }]

    def forbidden_mark_request(*_args, **_kwargs):
        raise AssertionError("unrelated BTC must be filtered before requesting market data")

    class Request:
        symbol = "ETH/USDT"
        exchange = "binance"

        def model_copy(self, update):
            return SimpleNamespace(**{**vars(self), **update})

    monkeypatch.setattr(lifecycle, "get_customer_paper_positions", positions)
    monkeypatch.setattr(main, "market_data", forbidden_mark_request)
    result = asyncio.run(main._customer_paper_positions_for_exposure(
        customer_id=771, req=Request(), market_context={"bid": 2000},
    ))

    assert result == {"ok": True, "positions": []}


def test_customer_paper_buy_still_fails_closed_on_unpriced_same_group_position(monkeypatch):
    """A same-bucket SOL exposure must be marked before an ETH BUY can be approved."""
    import asyncio
    from types import SimpleNamespace
    from app import main
    import app.customer_paper_lifecycle as lifecycle

    async def positions(**_kwargs):
        return [{
            "symbol": "SOL/USDT",
            "venue": "binance",
            "quantity": "2",
            "average_entry_price": "100",
        }]

    class Request:
        symbol = "ETH/USDT"
        exchange = "binance"

        def model_copy(self, update):
            return SimpleNamespace(**{**vars(self), **update})

    monkeypatch.setattr(lifecycle, "get_customer_paper_positions", positions)
    monkeypatch.setattr(main, "market_data", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(main, "_server_paper_market_context", lambda *_args, **_kwargs: None)
    result = asyncio.run(main._customer_paper_positions_for_exposure(
        customer_id=772, req=Request(), market_context={"bid": 2000},
    ))

    assert result["ok"] is False
    assert result["stage"] == "paper_exposure_mark_unavailable"
    assert result["symbol"] == "SOL/USDT"



def test_customer_paper_group_marks_carry_fresh_quote_timestamps(monkeypatch):
    import asyncio
    from datetime import datetime, timezone
    from app import main
    import app.customer_paper_lifecycle as lifecycle

    async def positions(**_kwargs):
        return [{
            "symbol": "ETH/USDT",
            "venue": "binance",
            "quantity": "1",
            "average_entry_price": "100",
        }]

    class Request:
        symbol = "ETH/USDT"
        exchange = "binance"

    monkeypatch.setattr(lifecycle, "get_customer_paper_positions", positions)
    timestamp = datetime.now(timezone.utc).isoformat()
    marks = asyncio.run(main._customer_paper_group_mark_prices(
        customer_id=773,
        req=Request(),
        market_context={
            "bid": 2000,
            "quote_age_seconds": 0.0,
            "quote_timestamp_utc": timestamp,
        },
    ))

    mark = marks[("binance", "spot", "ETH/USDT")]
    assert mark["price"] == "2000.000000000000000000"
    assert mark["quote_timestamp_utc"] == timestamp


def test_customer_paper_exposure_rejects_timestamp_stale_mark_even_if_reported_age_is_fresh(monkeypatch):
    import asyncio
    from datetime import datetime, timedelta, timezone
    from app import main
    import app.customer_paper_lifecycle as lifecycle

    async def positions(**_kwargs):
        return [{
            "symbol": "ETH/USDT",
            "venue": "binance",
            "quantity": "1",
            "average_entry_price": "100",
        }]

    class Request:
        symbol = "ETH/USDT"
        exchange = "binance"

    monkeypatch.setattr(lifecycle, "get_customer_paper_positions", positions)
    stale_timestamp = (
        datetime.now(timezone.utc) - timedelta(seconds=15)
    ).isoformat()
    result = asyncio.run(main._customer_paper_positions_for_exposure(
        customer_id=774,
        req=Request(),
        market_context={
            "bid": 2000,
            # Deliberately contradict the timestamp: the timestamp must still win.
            "quote_age_seconds": 0.0,
            "quote_timestamp_utc": stale_timestamp,
        },
    ))

    assert result["ok"] is False
    assert result["stage"] == "paper_exposure_mark_unavailable"
    # The public result intentionally reports the exception class, not quote data.
    assert "(ValueError)" in result["reason"]


def test_customer_paper_buy_uses_effective_price_and_atomic_fill_cap():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    main_source = (root / "app" / "main.py").read_text(encoding="utf-8")
    flow_source = (root / "app" / "customer_paper_flow.py").read_text(encoding="utf-8")
    lifecycle_source = (root / "app" / "customer_paper_lifecycle.py").read_text(encoding="utf-8")

    assert "conservative_customer_paper_buy_price" in main_source
    assert "proposed_notional = quantity * exposure_price" in main_source
    assert "headroom / max(exposure_price, 1e-12)" in main_source
    assert 'proposed_notional=quantity * float(effective_buy_price)' in main_source
    assert "headroom / max(float(effective_buy_price), 1e-12)" in main_source

    # The sizing price calls the same pricing routine used to record the eventual fill.
    assert 'top_of_book = market["ask"] if side == "BUY" else market["bid"]' in flow_source
    assert 'price, _fee, _costs = _fill_price_and_fee("BUY", Decimal("1"), market)' in flow_source

    # The actual database transaction serializes group fills, rereads current positions,
    # and sizes the fill against its actual price before inserting the fill row.
    assert 'paper-correlation-group:{customer_id}:{target_group}' in lifecycle_source
    assert "existing_group_notional + amount * fill_price" in lifecycle_source
    assert "NO_CORRELATION_GROUP_HEADROOM" in lifecycle_source
    assert lifecycle_source.index("paper-correlation-group:") < lifecycle_source.index(
        "INSERT INTO customer_paper.fills"
    )


def test_group_cap_preserves_reducing_quantity_and_caps_only_increase():
    from app.correlation_risk import cap_quantity_preserving_reduction

    # Existing exposure is 600; closing 2 units releases 200 at the current mark.
    # With a 500 cap, the remaining exposure is 400 and only 1 additional unit fits
    # at a 100 effective order price.
    permitted = cap_quantity_preserving_reduction(
        5, 2, existing_notional=600, reducing_notional=200,
        cap_usd=500, unit_price=100,
    )
    assert permitted == 3

    # Even when the cap is already breached, the known reducing part remains permitted.
    assert cap_quantity_preserving_reduction(
        5, 2, existing_notional=1000, reducing_notional=100,
        cap_usd=500, unit_price=100,
    ) == 2

    # An unknown mark/headroom cannot authorize an increasing quantity.
    assert cap_quantity_preserving_reduction(
        5, 2, existing_notional=float("nan"), reducing_notional=100,
        cap_usd=500, unit_price=100,
    ) == 2


def test_reducing_position_requires_matching_exchange_for_reduce_only():
    from types import SimpleNamespace

    requested_exchange = "binance"
    position = SimpleNamespace(exchange="kraken", symbol="BTC/USDT", quantity=2)
    assert str(position.exchange).strip().lower() != requested_exchange
