#!/usr/bin/env python3
"""Operator-run exchange SANDBOX drill (places tiny orders on the venue's testnet). Never run against mainnet.

Proves, on the real venue, the behaviours the ledger relies on:
  1. connectivity + market limits          4. duplicate clientOrderId does not double-fill
  2. where fee data actually appears        5. far limit order: open -> cancel -> double-cancel race
  3. market order fill, flatten             6. fee extraction vs the venue's own trade list

Usage:  python -m scripts.exchange_sandbox_drill --i-understand-sandbox [--exchange binance] [--symbol BTC/USDT] [--max-notional 50]
        python -m scripts.exchange_sandbox_drill --dry-run      # logic check with canned data, no network
Exit code 0 only if every executed step passed. SKIP is not PASS: a skipped step means that gate is still open.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid

from app.live_fees import extract_order_fee_quote, ESTIMATED

RESULTS: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    RESULTS.append((name, status, detail))
    print(f"[{status:4}] {name} {detail}")


def trades_fee_quote(trades: list[dict], symbol: str) -> float:
    total = 0.0
    for t in trades:
        fee, _, _ = extract_order_fee_quote({"fee": t.get("fee")}, symbol, float(t.get("price") or 0), float(t.get("amount") or 0), fallback_taker_bps=0.0)
        total += fee
    return total


def dry_run() -> int:
    order = {"id": "1", "status": "closed", "filled": 0.01, "average": 60000.0, "fee": {"currency": "USDT", "cost": 0.6}}
    trades = [{"price": 60000.0, "amount": 0.01, "fee": {"currency": "USDT", "cost": 0.6}}]
    fee, quality, _ = extract_order_fee_quote(order, "BTC/USDT", 60000.0, 0.01, fallback_taker_bps=5.5)
    record("dry-run: order fee parsed", "PASS" if abs(fee - 0.6) < 1e-9 else "FAIL", f"fee={fee} quality={quality}")
    record("dry-run: trades fee matches", "PASS" if abs(trades_fee_quote(trades, "BTC/USDT") - fee) < 1e-9 else "FAIL")
    fee2, q2, note = extract_order_fee_quote({"id": "2"}, "BTC/USDT", 60000.0, 0.01, fallback_taker_bps=5.5)
    record("dry-run: missing fee -> estimate", "PASS" if q2 == ESTIMATED and fee2 > 0 else "FAIL", note)
    return 0 if all(s == "PASS" for _, s, _ in RESULTS) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--i-understand-sandbox", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--exchange", default=os.getenv("DEFAULT_EXCHANGE", "binance"))
    ap.add_argument("--symbol", default=os.getenv("DEFAULT_SYMBOL", "BTC/USDT"))
    ap.add_argument("--max-notional", type=float, default=50.0)
    args = ap.parse_args()
    if args.dry_run:
        return dry_run()
    if not args.i_understand_sandbox:
        print("Refusing: pass --i-understand-sandbox (this script places real orders on the venue TESTNET).")
        return 2
    if os.getenv("ENVIRONMENT", "").lower() == "production":
        print("Refusing to run with ENVIRONMENT=production.")
        return 2
    from app.broker import Broker, BrokerConfig  # lazy: needs ccxt

    cfg = BrokerConfig(args.exchange, os.getenv("EXCHANGE_API_KEY", ""), os.getenv("EXCHANGE_API_SECRET", ""),
                       os.getenv("EXCHANGE_PASSWORD", ""), sandbox=True,
                       market_type=os.getenv("DEFAULT_MARKET_TYPE", "spot"), timeout_ms=10000)
    b = Broker.get(cfg)
    c = b._c()
    if not getattr(c, "urls", {}).get("test") and not getattr(c, "sandbox", False) and not getattr(c.options, "get", lambda *_: False)("sandboxMode"):
        print("Refusing: ccxt client does not report sandbox mode.")
        return 2
    sym = args.symbol

    # 1 connectivity + limits
    try:
        t = b.ticker(sym)
        px = float(t.get("last") or t.get("close") or 0)
        lim = b.market_info(sym).get("limits", {})
        record("connectivity + ticker", "PASS" if px > 0 else "FAIL", f"last={px}")
    except Exception as exc:
        record("connectivity + ticker", "FAIL", repr(exc))
        return 1
    min_amt = float(((lim.get("amount") or {}).get("min")) or 0)
    min_cost = float(((lim.get("cost") or {}).get("min")) or 0)
    qty = b.normalize_amount(sym, max(min_amt, (min_cost * 1.2 / px) if px else 0, 0.0001))
    if qty * px > args.max_notional:
        record("order size within --max-notional", "FAIL", f"{qty * px:.2f} > {args.max_notional}")
        return 1

    # 3 market order + 2 fee-data location
    cid = f"drill{uuid.uuid4().hex[:20]}"
    try:
        order = b.market_order(sym, "buy", qty, cid, None, None, False)
        fetched = b.fetch_order(str(order["id"]), sym)
        time.sleep(1.0)
        trades = c.fetch_my_trades(sym, limit=20)
        mine = [x for x in trades if str(x.get("order")) == str(order["id"])]
        has_create = bool(order.get("fee") or order.get("fees"))
        has_fetch = bool(fetched.get("fee") or fetched.get("fees"))
        record("market buy filled", "PASS" if float(fetched.get("filled") or 0) > 0 else "FAIL", f"filled={fetched.get('filled')}")
        record("fee present in create_order response", "PASS" if has_create else "WARN", "" if has_create else "fee only via fetch_order/my_trades -> estimates will be flagged")
        record("fee present in fetch_order response", "PASS" if has_fetch else "WARN")
        # 6 extraction vs venue trade list
        got, quality, note = extract_order_fee_quote(fetched, sym, float(fetched.get("average") or px), float(fetched.get("filled") or 0), fallback_taker_bps=0.0)
        truth = trades_fee_quote(mine, sym)
        ok = truth > 0 and abs(got - truth) <= max(1e-8, truth * 0.02) and quality != ESTIMATED
        record("fee extraction matches venue trades (<=2%)", "PASS" if ok else "FAIL", f"extracted={got} venue={truth} quality={quality} {note}")
    except Exception as exc:
        record("market buy / fee capture", "FAIL", repr(exc))
        return 1

    # 4 duplicate clientOrderId
    try:
        dup = b.market_order(sym, "buy", qty, cid, None, None, False)
        again = b.fetch_order(str(dup["id"]), sym) if dup.get("id") else {}
        same = str(dup.get("id")) == str(order.get("id"))
        record("duplicate clientOrderId did not create a second fill", "PASS" if same else "FAIL", f"first={order.get('id')} dup={dup.get('id')} filled={again.get('filled')}")
    except Exception as exc:  # exchange rejecting the duplicate is the desired outcome
        record("duplicate clientOrderId rejected by venue", "PASS", type(exc).__name__)

    # flatten
    try:
        b.market_order(sym, "sell", qty, f"drillx{uuid.uuid4().hex[:18]}", None, None, False)
        record("flatten position", "PASS")
    except Exception as exc:
        record("flatten position", "FAIL", repr(exc))

    # 5 far limit order -> open -> cancel -> double cancel
    try:
        far = b.normalize_price(sym, px * 0.5)
        lo = c.create_order(sym, "limit", "buy", qty, far, {"clientOrderId": f"drilll{uuid.uuid4().hex[:17]}"})
        opened = any(str(o.get("id")) == str(lo["id"]) for o in b.fetch_open_orders(sym))
        record("far limit order visible as open", "PASS" if opened else "FAIL")
        b.cancel_order(str(lo["id"]), sym)
        try:
            b.cancel_order(str(lo["id"]), sym)
            record("double-cancel raises OrderNotFound/closed", "WARN", "venue accepted second cancel silently")
        except Exception as exc:
            record("double-cancel raises OrderNotFound/closed", "PASS", type(exc).__name__)
    except Exception as exc:
        record("limit order open/cancel", "FAIL", repr(exc))

    record("worker-crash-mid-fill / timeout drill", "SKIP", "needs a staging worker kill; run the fake-broker tests + manual kill -9 of the worker during step 3")
    counts = {s: sum(1 for _, x, _ in RESULTS if x == s) for s in ("PASS", "WARN", "FAIL", "SKIP")}
    print("\nSummary:", counts)
    # A real sandbox gate is evidence-driven: WARN/SKIP are not successful completion.
    # In particular, the worker-crash step intentionally remains open until an operator
    # performs the staging kill/restart drill and records the result.
    return 0 if counts["FAIL"] == 0 and counts["WARN"] == 0 and counts["SKIP"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
