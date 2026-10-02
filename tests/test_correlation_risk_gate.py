"""correlation_risk.py had zero test coverage and its one function, adjusted_group_exposure(),
was imported in main.py but never called -- confirmed dead code during review. These tests
cover the function itself (previously untested) and verify it is now actually wired into the
live per-symbol decision pipeline in the correct position."""
import pytest

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
    """The gate must only look at *this* customer's open positions, never cross-customer."""
    src = open("app/main.py").read()
    i = src.index("group_check = adjusted_group_exposure(")
    preceding = src[max(0, i - 400):i]
    assert "Position.customer_id == profile.id" in preceding
    assert "Position.quantity != 0" in preceding
