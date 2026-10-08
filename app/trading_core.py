from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
from datetime import datetime, timezone
import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import balanced_accuracy_score, f1_score, log_loss
from lightgbm import LGBMClassifier
from .config import settings
from .research_validation import deflated_sharpe_report, cost_breakeven_report
from .model_registry import sha256_file, artifact_signature, verify_artifact_signature


@dataclass(frozen=True)
class Profile:
    bars_per_year: int
    taker_bps: float
    slippage_bps: float
    carry_bps_per_bar: float
    max_hold: int
    target_vol_annual: float
    max_leverage: float


PROFILES = {
    "crypto": Profile(8760, 5.5, 2.0, 0.125, 12, 0.30, 3.0),
    "forex": Profile(6240, 0.5, 0.5, 0.0, 24, 0.10, 10.0),
    # Commodity research currently reuses the existing research cost controls
    # and non-crypto risk defaults. These are deliberately not tuned here;
    # commodity-specific cost calibration is a later, evidence-gated step.
    "commodity": Profile(
        6240,
        settings.research_taker_bps,
        settings.research_slippage_bps,
        0.0,
        24,
        settings.strategy_target_vol_annual,
        settings.strategy_max_leverage,
    ),
}

FEATURE_COLUMNS = [
    "ret_1", "ret_4", "ret_12", "ret_24", "ret_72",
    "vol_1", "vol_4", "vol_12", "vol_24", "vol_72",
    "vol_ratio_24_72", "range", "range_z", "vol_z",
    "trend_50", "trend_200", "trend_slope_20", "rsi_14", "atr_pct_14", "drawdown_72",
    "volume_trend_24", "range_regime", "hour_sin", "hour_cos", "dow",
]


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    f = pd.DataFrame(index=df.index)
    close = df["close"].astype(float)
    logc = np.log(close)
    r = logc.diff()
    for h in (1, 4, 12, 24, 72):
        f[f"ret_{h}"] = logc.diff(h)
        f[f"vol_{h}"] = r.rolling(max(h, 2), min_periods=max(h, 2)).std()
    f["vol_ratio_24_72"] = f["vol_24"] / f["vol_72"].replace(0, np.nan)
    f["range"] = (df["high"] - df["low"]) / close
    range_std = f["range"].rolling(72).std()
    f["range_z"] = ((f["range"] - f["range"].rolling(72).mean()) / range_std.replace(0, np.nan)).fillna(0.0)
    vol = df["volume"].astype(float)
    vol_std = vol.rolling(72).std()
    f["vol_z"] = ((vol - vol.rolling(72).mean()) / vol_std.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    f["trend_50"] = close / close.rolling(50).mean() - 1
    f["trend_200"] = close / close.rolling(200).mean() - 1
    f["trend_slope_20"] = close.pct_change(20) / 20.0
    f["rsi_14"] = rsi(close, 14)
    tr = pd.concat([(df["high"] - df["low"]).abs(), (df["high"] - close.shift()).abs(), (df["low"] - close.shift()).abs()], axis=1).max(axis=1)
    f["atr_pct_14"] = tr.rolling(14).mean() / close
    f["drawdown_72"] = close / close.rolling(72).max() - 1.0
    f["volume_trend_24"] = vol / vol.rolling(24).mean().replace(0, np.nan) - 1.0
    f["range_regime"] = f["range"] / f["range"].rolling(72).mean().replace(0, np.nan)
    f["hour_sin"] = np.sin(2 * np.pi * df.index.hour / 24)
    f["hour_cos"] = np.cos(2 * np.pi * df.index.hour / 24)
    f["dow"] = df.index.dayofweek
    f = f.replace([np.inf, -np.inf], np.nan)
    # Keep the schema stable even if a caller supplies a reduced DataFrame.
    f = f[FEATURE_COLUMNS]
    return f.dropna()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    result = 100 - 100 / (1 + rs)
    both_zero = (up == 0) & (dn == 0)
    only_up = (up > 0) & (dn == 0)
    only_down = (up == 0) & (dn > 0)
    result = result.mask(both_zero, 50.0).mask(only_up, 100.0).mask(only_down, 0.0)
    return result


def triple_barrier(close: pd.Series, vol: pd.Series, high: pd.Series | None = None,
                   low: pd.Series | None = None, pt: float = 2.0, sl: float = 2.0,
                   max_hold: int = 12) -> pd.Series:
    c = close.to_numpy(dtype=float)
    v = vol.to_numpy(dtype=float)
    h = (high.reindex(close.index).to_numpy(dtype=float) if high is not None else c.copy())
    l = (low.reindex(close.index).to_numpy(dtype=float) if low is not None else c.copy())
    y = np.full(len(c), np.nan, dtype=float)
    for i in range(len(c) - max_hold):
        if not np.isfinite(v[i]) or v[i] <= 0 or not np.isfinite(c[i]):
            continue
        upper, lower = c[i] * (1 + pt * v[i]), c[i] * (1 - sl * v[i])
        end = min(len(c), i + max_hold + 1)
        for j in range(i + 1, end):
            up_hit = h[j] >= upper
            down_hit = l[j] <= lower
            if up_hit and down_hit:
                # Intrabar touch order is unknowable from OHLC alone: use conservative neutral label.
                y[i] = 0
                break
            if up_hit:
                y[i] = 1
                break
            if down_hit:
                y[i] = -1
                break
    return pd.Series(y, index=close.index, dtype="float64")


def _model_paths(model_path: str):
    p = Path(model_path)
    return p, p.with_suffix(p.suffix + ".meta.json")


def train_model(df: pd.DataFrame, model_path: str, min_train: int = 800, folds: int = 5, asset: str = "crypto") -> dict:
    X = build_features(df)
    aligned = df.reindex(X.index)
    profile = PROFILES.get(asset, PROFILES["crypto"])
    y = triple_barrier(aligned["close"], X["vol_24"], aligned["high"], aligned["low"], max_hold=profile.max_hold)
    mask = y.notna()
    X, y = X.loc[mask], y.loc[mask]
    if y.nunique() < 2:
        raise RuntimeError("Training target collapsed to fewer than two classes; adjust labeling or obtain richer market history")
    if len(X) < min_train + folds * (profile.max_hold + 20):
        raise RuntimeError(f"Not enough data for walk-forward training: {len(X)} samples")

    encoded = y.astype(int) + 1  # -1,0,1 -> 0,1,2
    tscv = TimeSeriesSplit(n_splits=folds, gap=profile.max_hold)
    scores = []
    models = []
    for tr_idx, te_idx in tscv.split(X):
        if len(tr_idx) < min_train or len(te_idx) == 0:
            continue
        train_classes = np.unique(encoded.iloc[tr_idx])
        if len(train_classes) < 2:
            continue
        model = LGBMClassifier(
            objective="multiclass",
            class_weight="balanced", n_estimators=300, max_depth=3,
            learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
            min_child_samples=50, reg_lambda=1.0, random_state=7,
            verbosity=-1, bagging_freq=1,
        )
        model.fit(X.iloc[tr_idx], encoded.iloc[tr_idx].to_numpy(dtype=int))
        pred = model.predict(X.iloc[te_idx])
        probs = model.predict_proba(X.iloc[te_idx])
        scores.append({
            "balanced_accuracy": float(balanced_accuracy_score(encoded.iloc[te_idx], pred)),
            "f1_macro": float(f1_score(encoded.iloc[te_idx], pred, average="macro", zero_division=0)),
            "log_loss": float(log_loss(encoded.iloc[te_idx], probs, labels=list(model.classes_))),
            "train_samples": int(len(tr_idx)),
            "test_samples": int(len(te_idx)),
        })
        models.append(model)
    if not models:
        raise RuntimeError("No valid walk-forward folds were produced with at least two target classes")
    if encoded.nunique() < 2:
        raise RuntimeError("Training target collapsed to fewer than two classes; adjust labeling or obtain richer market history")

    final_model = LGBMClassifier(
        objective="multiclass",
        class_weight="balanced", n_estimators=300, max_depth=3,
        learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
        min_child_samples=50, reg_lambda=1.0, random_state=7,
        verbosity=-1, bagging_freq=1,
    )
    final_model.fit(X, encoded.to_numpy(dtype=int))
    model_file, meta_file = _model_paths(model_path)
    model_file.parent.mkdir(parents=True, exist_ok=True)
    import joblib
    joblib.dump(final_model, model_file)
    model_hash = sha256_file(model_file)
    manifest_signature = artifact_signature(model_hash) if (settings.model_signing_private_key or settings.model_signing_key) else ""
    meta = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "samples": int(len(X)),
        "features": FEATURE_COLUMNS,
        "classes": [-1, 0, 1],
        "validation": scores,
        "model_sha256": model_hash,
        "manifest_signature": manifest_signature,
        "training_start": X.index.min().isoformat(),
        "training_end": X.index.max().isoformat(),
        "asset": asset,
        "max_hold": profile.max_hold,
    }
    meta_file.write_text(json.dumps(meta, indent=2))
    return {"samples": len(X), "features": len(FEATURE_COLUMNS), "folds": len(scores), "validation": scores, "model_sha256": model_hash}


def load_model(model_path: str):
    import joblib
    model_file, meta_file = _model_paths(model_path)
    if not model_file.exists() or not meta_file.exists():
        raise FileNotFoundError(model_file)
    meta = json.loads(meta_file.read_text())
    # Read the artifact ONCE: hash, verify and deserialize the same bytes so the file cannot be
    # swapped between verification and joblib.load (joblib is pickle -> code execution on load).
    model_bytes = model_file.read_bytes()
    digest = hashlib.sha256(model_bytes).hexdigest()
    if digest != meta.get("model_sha256"):
        raise RuntimeError("Model artifact hash mismatch")
    if settings.environment in {"staging", "production"} and settings.model_integrity_required_for_live:
        expected_signature = str(meta.get("manifest_signature") or "")
        if not (settings.model_signing_public_key or settings.model_signing_key) or not expected_signature:
            raise RuntimeError("Model artifact signature is required in staging/production")
        if not verify_artifact_signature(digest, expected_signature):
            raise RuntimeError("Model artifact signature mismatch")
    import io
    model = joblib.load(io.BytesIO(model_bytes))
    return model, meta


def predict_latest(df: pd.DataFrame, model_path: str, threshold: float = 0.05) -> dict:
    model, meta = load_model(model_path)
    X = build_features(df)
    if X.empty:
        raise RuntimeError("Insufficient recent data")
    row = X.iloc[[-1]]
    probs_raw = np.asarray(model.predict_proba(row))[0]
    mapped = {int(cls): float(p) for cls, p in zip(model.classes_, probs_raw, strict=True)}
    short_p = mapped.get(0, 0.0)
    flat_p = mapped.get(1, 0.0)
    long_p = mapped.get(2, 0.0)
    total = short_p + flat_p + long_p
    if not np.isfinite(total) or total <= 0:
        raise RuntimeError("Invalid model probabilities")
    short_p, flat_p, long_p = [x / total for x in (short_p, flat_p, long_p)]
    score = long_p - short_p
    signal = 1 if score >= threshold else (-1 if score <= -threshold else 0)
    return {
        "model_version": meta.get("model_sha256", ""),
        "signal_timestamp": X.index[-1].isoformat(),
        "long_probability": long_p,
        "flat_probability": flat_p,
        "short_probability": short_p,
        "score": float(score),
        "signal": signal,
    }


def _research_bars_per_year(index: pd.Index, asset: str, fallback: int) -> float:
    """Annualize research returns from the actual bar spacing, not a fixed profile."""
    if len(index) < 3 or not isinstance(index, pd.DatetimeIndex):
        return float(fallback)
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    seconds = idx.to_series().diff().dt.total_seconds().dropna()
    seconds = seconds[(seconds > 0) & np.isfinite(seconds)]
    if seconds.empty:
        return float(fallback)
    days_per_year = 365.25 if str(asset).lower() == "crypto" else 252.0
    return float(np.clip((days_per_year * 24 * 3600) / float(seconds.median()), 1.0, 1_000_000.0))


def _simulate_barrier_strategy(
    df: pd.DataFrame,
    vol: pd.Series,
    signals: pd.Series,
    max_hold: int,
    pt: float = 2.0,
    sl: float = 2.0,
    taker_bps: float = 0.0,
    slippage_bps: float = 0.0,
    carry_bps_per_bar: float = 0.0,
    end: int | None = None,
) -> dict:
    """Execute predicted signals with the same triple-barrier concept used for labels.

    Signals are generated at bar close t and enter at the next bar open. Barrier
    levels are anchored to close[t], matching the research label. If the next
    open gaps through a barrier, execution occurs at that open. If both barriers
    are touched inside one OHLC bar, the stop is taken conservatively.
    """
    n = len(df)
    stop_at = min(n, end if end is not None else n)
    close = df["close"].to_numpy(dtype=float)
    open_ = df["open"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    v = vol.reindex(df.index).to_numpy(dtype=float)
    sig = signals.reindex(df.index).to_numpy(dtype=float)
    net = np.zeros(n, dtype=float)
    trade_count = 0
    entry_count = 0
    exit_count = 0
    i = 0
    cost_rate = (float(taker_bps) + float(slippage_bps)) / 10_000.0

    while i < stop_at - 1:
        if not np.isfinite(sig[i]) or sig[i] == 0 or not np.isfinite(v[i]) or v[i] <= 0:
            i += 1
            continue
        side = 1 if sig[i] > 0 else -1
        entry_bar = i + 1
        if entry_bar >= stop_at or not np.isfinite(open_[entry_bar]):
            break
        anchor = close[i]
        entry_price = open_[entry_bar]
        upper = anchor * (1.0 + pt * v[i])
        lower = anchor * (1.0 - sl * v[i])
        if not np.isfinite(anchor) or not np.isfinite(entry_price) or upper <= 0 or lower <= 0:
            i += 1
            continue

        entry_count += 1
        trade_count += 1
        # Entry transaction cost is charged on the entry bar.
        net[entry_bar] -= cost_rate
        exit_bar = min(stop_at - 1, entry_bar + max_hold - 1)
        prev_mark = entry_price
        exited = False

        for j in range(entry_bar, exit_bar + 1):
            if not np.isfinite(close[j]) or not np.isfinite(high[j]) or not np.isfinite(low[j]):
                continue
            exit_price = None
            # Gap through the protective barrier is filled at the open.
            if side > 0 and open_[j] <= lower:
                exit_price = open_[j]
            elif side < 0 and open_[j] >= upper:
                exit_price = open_[j]
            else:
                up_hit = high[j] >= upper
                down_hit = low[j] <= lower
                if up_hit and down_hit:
                    exit_price = lower if side > 0 else upper
                elif side > 0 and down_hit:
                    exit_price = lower
                elif side < 0 and up_hit:
                    exit_price = upper
                elif j == exit_bar:
                    exit_price = close[j]

            mark = exit_price if exit_price is not None else close[j]
            if side > 0:
                bar_ret = mark / prev_mark - 1.0
            else:
                bar_ret = prev_mark / mark - 1.0
            if np.isfinite(bar_ret):
                net[j] += bar_ret
            held_bars = j - entry_bar + 1
            net[j] -= abs(side) * float(carry_bps_per_bar) / 10_000.0

            if exit_price is not None:
                net[j] -= cost_rate
                exit_count += 1
                exited = True
                i = j + 1
                break
            prev_mark = close[j]

        if not exited:
            i = exit_bar + 1

    return {
        "net": pd.Series(net, index=df.index),
        "trades": int(trade_count),
        "entries": int(entry_count),
        "exits": int(exit_count),
    }


def ai_walk_forward_backtest(
    df: pd.DataFrame,
    asset: str = "crypto",
    folds: int = 5,
    min_train: int = 800,
    threshold: float = 0.05,
    cost_stress_multiplier: float = 2.0,
) -> dict:
    """Walk-forward AI research using execution-aware triple-barrier exits.

    This deliberately remains research-only. It fixes the previous mismatch in
    which the model was trained on triple-barrier outcomes but evaluated as a
    signal-hold strategy with no corresponding stop/target execution.
    """
    if not np.isfinite(float(cost_stress_multiplier)) or float(cost_stress_multiplier) <= 0:
        raise ValueError("cost_stress_multiplier must be a finite positive number")
    if not np.isfinite(float(threshold)) or float(threshold) < 0:
        raise ValueError("threshold must be finite and >= 0")
    X = build_features(df)
    aligned = df.reindex(X.index)
    profile = PROFILES[asset]
    y = triple_barrier(aligned.close, X.vol_24, aligned.high, aligned.low, max_hold=profile.max_hold)
    valid = y.notna()
    X, aligned, y = X.loc[valid], aligned.loc[valid], y.loc[valid]
    if len(X) < min_train + folds * (profile.max_hold + 20):
        raise RuntimeError("Insufficient data for AI walk-forward backtest")

    tscv = TimeSeriesSplit(n_splits=folds, gap=profile.max_hold)
    scores = []
    fold_metrics = []
    pred_signal = pd.Series(np.nan, index=X.index)

    for tr, te in tscv.split(X):
        if len(tr) < min_train or len(np.unique((y.iloc[tr] + 1).to_numpy())) < 2:
            continue
        m = LGBMClassifier(
            objective="multiclass", class_weight="balanced",
            n_estimators=300, max_depth=3, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, min_child_samples=50,
            reg_lambda=1.0, random_state=7, verbosity=-1, bagging_freq=1,
        )
        m.fit(X.iloc[tr], (y.iloc[tr] + 1).to_numpy(dtype=int))
        p = m.predict_proba(X.iloc[te])
        mp = {int(cls): p[:, i] for i, cls in enumerate(m.classes_)}
        long_p = mp.get(2, np.zeros(len(te)))
        short_p = mp.get(0, np.zeros(len(te)))
        score = long_p - short_p
        sig = np.where(score >= threshold, 1, np.where(score <= -threshold, -1, 0))
        pred_signal.iloc[te] = sig
        scores.extend(score.tolist())

        # Evaluate only inside the current OOS fold so a trade cannot consume
        # future observations belonging to another fold.
        fold_signals = pd.Series(np.nan, index=X.index)
        fold_signals.iloc[te] = sig
        sim = _simulate_barrier_strategy(
            aligned, X.vol_24, fold_signals, profile.max_hold,
            pt=2.0, sl=2.0,
            taker_bps=profile.taker_bps,
            slippage_bps=profile.slippage_bps,
            carry_bps_per_bar=profile.carry_bps_per_bar,
            end=int(te[-1]) + 1,
        )
        fold_net = sim["net"].iloc[te].fillna(0.0)
        fold_equity = (1.0 + fold_net).cumprod()
        fold_peak = fold_equity.cummax()
        fold_dd = fold_equity / fold_peak - 1.0
        annual = _research_bars_per_year(aligned.index[te], asset, profile.bars_per_year)
        vol = float(fold_net.std())
        fold_sharpe = float(fold_net.mean() / vol * np.sqrt(annual)) if vol > 0 else 0.0
        fold_metrics.append({
            "train_bars": int(len(tr)),
            "test_bars": int(len(te)),
            "total_return": float(fold_equity.iloc[-1] - 1.0),
            "max_drawdown": float(fold_dd.min()),
            "trades": int(sim["trades"]),
            "sharpe": fold_sharpe,
        })

    sim = _simulate_barrier_strategy(
        aligned, X.vol_24, pred_signal, profile.max_hold,
        pt=2.0, sl=2.0,
        taker_bps=profile.taker_bps,
        slippage_bps=profile.slippage_bps,
        carry_bps_per_bar=profile.carry_bps_per_bar,
    )
    net = sim["net"]
    equity = (1.0 + net).cumprod()
    peak = equity.cummax()
    dd = equity / peak - 1.0
    annual = _research_bars_per_year(aligned.index, asset, profile.bars_per_year)
    cost_report = cost_breakeven_report(
        np.zeros(len(net)), np.zeros(len(net)),
        taker_bps=profile.taker_bps,
        slippage_bps=profile.slippage_bps,
        bars_per_year=annual,
        stress_multiplier=cost_stress_multiplier,
    )
    # Cost breakeven is meaningful only when gross trade returns are supplied;
    # execution-aware simulation already includes actual configured costs.
    cost_report["execution_aware"] = True
    cost_report["note"] = "Gross-cost stress is evaluated by replaying the same exits with multiplied transaction costs."
    dsr_report = deflated_sharpe_report(
        net.to_numpy(),
        [float(f.get("sharpe", 0.0)) for f in fold_metrics],
        max(1, len(fold_metrics)),
        annual,
    )
    vol = float(net.std())
    downside = net[net < 0].std()
    sharpe = float(net.mean() / vol * np.sqrt(annual)) if vol > 0 else 0.0
    sortino = float(net.mean() / downside * np.sqrt(annual)) if downside and np.isfinite(downside) and downside > 0 else 0.0
    max_dd = float(dd.min())
    calmar = float((equity.iloc[-1] - 1.0) / abs(max_dd)) if max_dd < 0 else 0.0
    active = net != 0
    wins = net[active][net[active] > 0].sum()
    losses = -net[active][net[active] < 0].sum()
    profit_factor = float(wins / losses) if losses > 0 else (float("inf") if wins > 0 else 0.0)

    return {
        "bars": int(len(df)),
        "features": int(X.shape[1]),
        "validated_bars": int(pred_signal.notna().sum()),
        "total_return": float(equity.iloc[-1] - 1.0),
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "profit_factor": profit_factor,
        "trades": int(sim["trades"]),
        "entries": int(sim["entries"]),
        "exits": int(sim["exits"]),
        "hit_rate": float((net[active] > 0).mean()) if bool(active.any()) else 0.0,
        "average_active_bar_return": float(net[active].mean()) if bool(active.any()) else 0.0,
        "validation_signal_count": len(scores),
        "prediction_gap_policy": "NO_FORWARD_FILL_ACROSS_NON_TEST_WINDOWS",
        "execution_model": "NEXT_OPEN_TRIPLE_BARRIER_WITH_CONSERVATIVE_SAME_BAR_STOP",
        "barrier_pt": 2.0,
        "barrier_sl": 2.0,
        "barrier_max_hold": int(profile.max_hold),
        "annualization_factor": annual,
        "folds": fold_metrics,
        # Replayed execution is the authoritative cost result. The old analytical
        # cost-breakeven report assumed signal-hold returns and was not valid for
        # barrier exits, so it is retained only as metadata.
        "cost_stress_ok": False,
        "cost_stress_multiplier": float(cost_stress_multiplier),
        "cost_analysis": cost_report,
        "deflated_sharpe": dsr_report.get("deflated_sharpe_ratio") if dsr_report.get("status") == "OK" else None,
        "deflated_sharpe_status": dsr_report.get("status"),
    }
