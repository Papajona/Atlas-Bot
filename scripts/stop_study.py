"""Offline stop/exit study on an OHLCV CSV.

    python -m scripts.stop_study --csv data.csv --asset crypto --signal ensemble
"""
from __future__ import annotations
import argparse
import json
import pandas as pd
from app.strategy_exits import stop_policy_study

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--asset", default="crypto")
    ap.add_argument("--signal", default="ensemble", choices=["ensemble","trend","momentum","breakout","mean_reversion"])
    ap.add_argument("--taker-bps", type=float, default=5.5)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--stop-extra-slippage-bps", type=float, default=0.0)
    args = ap.parse_args(argv)
    df = pd.read_csv(args.csv, parse_dates=["timestamp"]).set_index("timestamp").sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    out = stop_policy_study(df, asset=args.asset, signal=args.signal, taker_bps=args.taker_bps,
                            slippage_bps=args.slippage_bps, stop_extra_slippage_bps=args.stop_extra_slippage_bps)
    print(json.dumps(out, indent=2, default=str))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
