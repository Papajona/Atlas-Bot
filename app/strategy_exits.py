"""Exit-rule simulation for the strategy ensemble (stop-loss, take-profit, time stop).

Why this exists: strategy_engine.strategy_backtest states that it is "not intrabar stop-target execution", so the
ATR stop/target that strategy_signal attaches to live orders (2.5 / 4.0 ATR by default) has never been part of the
research evidence.

Conventions (deliberately conservative):
  * a position held during bar t is entered at open[t]; stop/target levels come from entry price and ATR known BEFORE the bar;
  * if one bar touches both the stop and the target, the stop is assumed to fill first;
  * a bar that opens through the stop fills at the open (gap), not at the stop price;
  * after a rule exit the same-direction signal is ignored until the signal goes flat or flips (no instant re-entry);
  * with every rule off the simulation is intended to reproduce strategy_backtest exactly.
Nothing here selects a stop automatically: the study reports every variant and a deflated Sharpe for the best one.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from itertools import product

import numpy as np
import pandas as pd

from .research_validation import deflated_sharpe_report
from .strategy_engine import StrategyConfig, _annualization_factor, strategy_signals


def target_positions(df: pd.DataFrame, cfg: StrategyConfig | None = None, asset: str = "crypto", signal: str = "ensemble"):
    cfg = cfg or StrategyConfig()
    s = strategy_signals(df, cfg, asset=asset)
    raw = s[signal].fillna(0.0)
    lev = (cfg.target_vol_annual / s["realized_vol"].replace(0, np.nan)).clip(upper=cfg.max_leverage)
    pos = (raw * lev).clip(-cfg.max_leverage, cfg.max_leverage)
    pos = pos.where(raw.abs() >= cfg.signal_threshold, 0.0).shift(1).fillna(0.0)
    return pos, s["atr"]


def simulate_exit_rules(df: pd.DataFrame, pos: pd.Series, atr: pd.Series, *, stop_atr: float | None = None,
                        take_profit_atr: float | None = None, max_hold_bars: int | None = None,
                        taker_bps: float = 5.5, slippage_bps: float = 2.0, stop_extra_slippage_bps: float = 0.0) -> dict:
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    n = len(o)
    p = pos.reindex(df.index).fillna(0.0).to_numpy(float)
    atr_prev = atr.reindex(df.index).shift(1).to_numpy(float)
    nxt = np.append(o[1:], o[-1])
    cost = (taker_bps + slippage_bps) / 10_000.0
    extra = stop_extra_slippage_bps / 10_000.0

    net = np.zeros(n)
    end_pos = 0.0
    blocked = 0.0
    entry = stop = tp = np.nan
    held = 0
    counts = {"entries": 0, "stop_exits": 0, "take_profit_exits": 0, "time_exits": 0}
    held_total = 0

    for t in range(n):
        tsign = float(np.sign(p[t]))
        if blocked != 0.0 and tsign != blocked:
            blocked = 0.0
        a = 0.0 if (blocked != 0.0 and tsign == blocked) else float(p[t])
        asign = float(np.sign(a))
        bar_cost = abs(a - end_pos) * cost
        if asign == 0.0:
            entry = stop = tp = np.nan
            held = 0
            end_pos = 0.0
            net[t] = -bar_cost
            continue
        if float(np.sign(end_pos)) != asign:
            counts["entries"] += 1
            entry, held = o[t], 0
            a_e = atr_prev[t]
            stop = tp = np.nan
            if np.isfinite(a_e) and a_e > 0:
                if stop_atr:
                    stop = entry - asign * stop_atr * a_e
                if take_profit_atr:
                    tp = entry + asign * take_profit_atr * a_e
        held += 1
        held_total += 1
        exit_px, reason = None, None
        if asign > 0:
            if np.isfinite(stop) and l[t] <= stop:
                exit_px, reason = (o[t] if o[t] <= stop else stop), "stop"
            elif np.isfinite(tp) and h[t] >= tp:
                exit_px, reason = (o[t] if o[t] >= tp else tp), "take_profit"
        else:
            if np.isfinite(stop) and h[t] >= stop:
                exit_px, reason = (o[t] if o[t] >= stop else stop), "stop"
            elif np.isfinite(tp) and l[t] <= tp:
                exit_px, reason = (o[t] if o[t] <= tp else tp), "take_profit"
        if exit_px is not None:
            net[t] = a * (exit_px / o[t] - 1.0) - bar_cost - abs(a) * cost - (abs(a) * extra if reason == "stop" else 0.0)
            counts["stop_exits" if reason == "stop" else "take_profit_exits"] += 1
            blocked, end_pos = asign, 0.0
            entry = stop = tp = np.nan
            held = 0
            continue
        net[t] = a * (nxt[t] / o[t] - 1.0) - bar_cost
        end_pos = a
        if max_hold_bars and held >= int(max_hold_bars):
            net[t] -= abs(a) * cost
            counts["time_exits"] += 1
            blocked, end_pos = asign, 0.0
            entry = stop = tp = np.nan
            held = 0
    return {"net": pd.Series(net, index=df.index), "counts": counts, "bars_in_market": held_total}


def _metrics(net: pd.Series, df: pd.DataFrame, asset: str, counts: dict) -> dict:
    equity = (1.0 + net).cumprod()
    dd = equity / equity.cummax() - 1.0
    ann = _annualization_factor(df.index, asset=asset)
    sd = float(net.std())
    return {"total_return": float(equity.iloc[-1] - 1.0), "max_drawdown": float(dd.min()),
            "sharpe": float(net.mean() / sd * np.sqrt(ann)) if sd > 0 else 0.0, **counts}


def stop_policy_study(df: pd.DataFrame, cfg: StrategyConfig | None = None, *, asset: str = "crypto", signal: str = "ensemble",
                      stop_grid=(1.5, 2.0, 2.5, 3.0, 4.0), take_profit_grid=(None, 4.0), max_hold_grid=(None,),
                      taker_bps: float = 5.5, slippage_bps: float = 2.0, stop_extra_slippage_bps: float = 0.0,
                      prior_trial_labels=None) -> dict:
    cfg = cfg or StrategyConfig()
    pos, atr = target_positions(df, cfg, asset=asset, signal=signal)
    fp = hashlib.sha1(json.dumps(asdict(cfg), sort_keys=True, default=str).encode(), usedforsecurity=False).hexdigest()[:8]
    runs, nets = [], {}
    for stop, tp, hold in [(None, None, None)] + [v for v in product(stop_grid, take_profit_grid, max_hold_grid) if any(x is not None for x in v)]:
        sim = simulate_exit_rules(df, pos, atr, stop_atr=stop, take_profit_atr=tp, max_hold_bars=hold, taker_bps=taker_bps,
                                  slippage_bps=slippage_bps, stop_extra_slippage_bps=stop_extra_slippage_bps)
        label = f"exit:{signal}:stop{stop}:tp{tp}:hold{hold}@{fp}"
        nets[label] = sim["net"]
        runs.append({"label": label, "stop_atr": stop, "take_profit_atr": tp, "max_hold_bars": hold,
                     **_metrics(sim["net"], df, asset, sim["counts"])})
    baseline = next(r for r in runs if r["stop_atr"] is None and r["take_profit_atr"] is None and r["max_hold_bars"] is None)
    ranked = sorted(runs, key=lambda r: r["sharpe"], reverse=True)
    best = ranked[0]
    labels = [r["label"] for r in runs]
    n_trials = len(set(prior_trial_labels or ()) | set(labels))
    dsr = deflated_sharpe_report(nets[best["label"]].to_numpy(), [r["sharpe"] for r in runs], n_trials, int(_annualization_factor(df.index, asset=asset)))
    return {"signal": signal, "baseline": baseline, "best": best, "variants": ranked,
            "best_is_baseline": best["label"] == baseline["label"], "deflated_sharpe": dsr,
            "trial_labels": labels, "cumulative_trials": n_trials,
            "note": "No rule is selected automatically. Adopt an exit rule only if it beats the baseline out of sample AND the "
                    "deflated Sharpe stays high after counting every variant tried (pass trial_labels back as prior_trial_labels)."}
