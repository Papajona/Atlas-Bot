#!/usr/bin/env python3
"""Opt-in Binance Spot Testnet acceptance drill.

This external-runtime harness is deliberately unable to target production:
it requires ATLAS_BINANCE_TESTNET=true and verifies the CCXT URLs contain
testnet.binance.vision before submitting an order.
"""
from __future__ import annotations
import os, time, uuid
from decimal import Decimal
from typing import Any
from types import SimpleNamespace
from app.customer_binance_execution import build_customer_binance_broker

def req(name: str) -> str:
    value=os.getenv(name,"").strip()
    if not value: raise SystemExit(f"Missing required environment variable: {name}")
    return value

def dec(v: Any) -> Decimal: return Decimal(str(v))

def main() -> int:
    if os.getenv("ATLAS_BINANCE_TESTNET","").strip().lower()!="true":
        raise SystemExit("Set ATLAS_BINANCE_TESTNET=true to run the external testnet drill")
    key=req("ATLAS_TESTNET_BINANCE_API_KEY"); secret=req("ATLAS_TESTNET_BINANCE_API_SECRET")
    symbol=os.getenv("ATLAS_TESTNET_SYMBOL","BTC/USDT").strip()
    account=SimpleNamespace(status="VERIFIED",can_trade=True,universal_transfer=False,
        enable_withdrawals=False,api_key=key,secret_ref="testnet-env-secret",market_type="spot")
    broker=build_customer_binance_broker(account,secret_fetcher=lambda _: secret,sandbox=True)
    client=broker.connect()
    if "testnet.binance.vision" not in str(client.urls): raise RuntimeError("Refusing non-Testnet Binance endpoint")
    if not broker.config.sandbox: raise RuntimeError("Refusing non-sandbox broker configuration")

    market=broker.market_info(symbol); ticker=broker.ticker(symbol); last=dec(ticker["last"])
    limits=market.get("limits") or {}
    min_cost=dec(((limits.get("cost") or {}).get("min")) or "0")
    min_amount=dec(((limits.get("amount") or {}).get("min")) or "0")
    budget=max(Decimal("15"),min_cost*Decimal("1.25"))
    amount=dec(broker.normalize_amount(symbol,float(max(min_amount,budget/last))))
    if amount<=0 or amount*last<min_cost: raise RuntimeError(f"Invalid test amount {amount}; price={last}; min_cost={min_cost}")

    report={"environment":"BINANCE_SPOT_TESTNET","symbol":symbol,"steps":[]}
    report["steps"].append({"step":"authenticated_balance","ok":True})
    order=broker.market_order(symbol,"buy",float(amount),"atlas-tn-"+uuid.uuid4().hex[:20])
    oid=str(order["id"]); deadline=time.time()+30
    while time.time()<deadline:
        order=broker.fetch_order(oid,symbol)
        if str(order.get("status")).lower() in {"closed","filled"}: break
        time.sleep(1)
    if str(order.get("status")).lower() not in {"closed","filled"} or dec(order.get("filled") or "0")<=0:
        raise RuntimeError(f"Testnet market order did not fill: {order}")
    report["steps"].append({"step":"spot_market_fill","ok":True,"order_id":oid,"filled":str(order.get("filled"))})

    order_types=set((market.get("info") or {}).get("orderTypes") or [])
    if "STOP_LOSS" not in order_types:
        raise RuntimeError("Binance exchangeInfo does not advertise STOP_LOSS for this Spot symbol; refusing a false protective-stop PASS")
    report["steps"].append({"step":"protective_stop_capability","ok":True,"order_type":"STOP_LOSS"})

    filled=dec(order["filled"])
    stop_price=dec(broker.normalize_price(symbol,float(last*Decimal("0.80"))))
    stop=client.create_order(
        symbol,
        "STOP_LOSS",
        "sell",
        float(dec(broker.normalize_amount(symbol,float(filled)))),
        None,
        {"stopPrice":float(stop_price),"clientOrderId":"atlas-sl-"+uuid.uuid4().hex[:20]},
    )
    stop_id=str(stop["id"])
    report["steps"].append({"step":"protective_stop_created","ok":True,"order_id":stop_id})

    client.cancel_order(stop_id,symbol)
    base=symbol.split("/")[0]
    remaining=dec(broker.fetch_balance().get("free",{}).get(base,"0"))
    if remaining>0:
        flat_amt=dec(broker.normalize_amount(symbol,float(remaining)))
        if flat_amt>0:
            flat=broker.market_order(symbol,"sell",float(flat_amt),"atlas-flat-"+uuid.uuid4().hex[:20])
            if str(flat.get("status")).lower() not in {"closed","filled"}: raise RuntimeError(f"Spot flatten did not fill: {flat}")
    final=dec(broker.fetch_balance().get("free",{}).get(base,"0"))
    report["steps"].append({"step":"emergency_cancel_and_spot_flatten","ok":final<=Decimal("0.00000001"),"remaining_base":str(final)})
    if final>Decimal("0.00000001"): raise RuntimeError(f"Residual Spot base balance: {final}")
    print(report); return 0

if __name__=="__main__": raise SystemExit(main())
