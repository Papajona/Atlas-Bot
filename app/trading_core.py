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
from .execution_costs import net_return_after_costs
from .research_validation import (
    deflated_sharpe_report, cost_breakeven_report, purged_walk_forward_splits,
    newey_west_mean_inference, oos_benchmark_comparison,
)
from .model_registry import sha256_file, artifact_signature, verify_artifact_signature
from .execution_aware_research import simulate_execution_aware_strategy


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


def triple_barrier(
    close: pd.Series,
    vol: pd.Series,
    high: pd.Series | None = None,
    low: pd.Series | None = None,
    pt: float = 2.0,
    sl: float = 2.0,
    max_hold: int = 12,
    *,
    entry_open: pd.Series | None = None,
    entry_atr: pd.Series | None = None,
    stop_atr: float | None = None,
    take_profit_atr: float | None = None,
    round_trip_cost_bps: float | pd.Series = 0.0,
    execution_cost_context: dict[str, object] | None = None,
    execution_cost_namespace: str = "ai-triple-barrier",
) -> pd.Series:
    """Create forward labels with the same execution-cost evidence as OOS simulation.

    The signal is known at close[i]; entry is open[i+1]. If a verified per-bar
    execution context is supplied, both entry and realized exit are priced as
    separate orders using that bar's spread, prior volume, volatility and latency.
    Static/session cost schedules remain available to legacy diagnostic callers.
    """
    c = close.to_numpy(dtype=float)
    v = vol.reindex(close.index).to_numpy(dtype=float)
    h = high.reindex(close.index).to_numpy(dtype=float) if high is not None else c.copy()
    l = low.reindex(close.index).to_numpy(dtype=float) if low is not None else c.copy()
    has_executable_open = entry_open is not None
    opens = (
        entry_open.reindex(close.index).to_numpy(dtype=float)
        if has_executable_open else c.copy()
    )
    has_execution_atr = entry_atr is not None
    entry_atr_values = (
        entry_atr.reindex(close.index).to_numpy(dtype=float)
        if has_execution_atr else np.full(len(c), np.nan, dtype=float)
    )
    y = np.full(len(c), np.nan, dtype=float)
    session_cost_schedule = isinstance(round_trip_cost_bps, pd.Series)
    try:
        hold = int(max_hold)
        if session_cost_schedule:
            if not round_trip_cost_bps.index.equals(close.index):
                raise ValueError("round-trip session costs must align exactly to close index")
            cost_bps = round_trip_cost_bps.to_numpy(dtype=float)
        else:
            cost_bps = np.full(len(c), float(round_trip_cost_bps), dtype=float)
        pt_mult, sl_mult = float(pt), float(sl)
        stop_mult = None if stop_atr is None else float(stop_atr)
        target_mult = None if take_profit_atr is None else float(take_profit_atr)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid triple-barrier configuration") from exc
    if isinstance(max_hold, bool) or hold != max_hold or hold < 1:
        raise ValueError("max_hold must be a positive integer")
    if (
        not np.isfinite(cost_bps).all() or np.any(cost_bps < 0)
        or not np.isfinite([pt_mult, sl_mult]).all()
        or pt_mult <= 0 or sl_mult <= 0
    ):
        raise ValueError("barriers and round-trip costs must be finite and positive/non-negative")
    if has_execution_atr and (
        not has_executable_open
        or stop_mult is None or target_mult is None
        or not np.isfinite([stop_mult, target_mult]).all()
        or stop_mult <= 0 or target_mult <= 0
    ):
        raise ValueError("execution-aligned labels require next-open and positive ATR stop/target")
    if not (len(v) == len(h) == len(l) == len(opens) == len(entry_atr_values) == len(c)):
        raise ValueError("triple-barrier inputs must align to close index")

    execution_cost_failures: dict[str, int] = {}
    execution_cost_pairs_priced = 0
    first_cost_ready_bar = 0
    if execution_cost_context is not None:
        from .research_execution_costs import execution_cost_for_event

        context_index = execution_cost_context.get("index")
        if not isinstance(context_index, pd.Index) or not context_index.equals(close.index):
            raise ValueError("per-bar execution context must align exactly with triple-barrier labels")
        try:
            context_spreads = np.asarray(execution_cost_context["spread_bps"], dtype=float)
            context_volume = np.asarray(execution_cost_context["volume_units"], dtype=float)
            context_volatility = np.asarray(execution_cost_context["volatility_bps"], dtype=float)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("per-bar execution context is missing valid market arrays") from exc
        if not (
            context_spreads.shape == (len(c),)
            and context_volume.shape == (len(c),)
            and context_volatility.shape == (len(c),)
        ):
            raise ValueError("per-bar execution context arrays must align exactly with triple-barrier labels")
        ready = (
            np.isfinite(context_spreads) & (context_spreads >= 0.0)
            & np.isfinite(context_volume) & (context_volume > 0.0)
            & np.isfinite(context_volatility) & (context_volatility >= 0.0)
        )
        ready_positions = np.flatnonzero(ready)
        first_cost_ready_bar = int(ready_positions[0]) if len(ready_positions) else len(c)

    for i in range(len(c) - hold):
        entry_i = i + 1
        # The evidence-bound context intentionally has a prior-volume warm-up.
        # Do not create labels before the first bar with causal cost observations.
        if execution_cost_context is not None and entry_i < first_cost_ready_bar:
            continue
        # Older offline callers without OHLC open data retain the historical close[i]
        # entry anchor. Production training and walk-forward callers supply next-open.
        entry_price = opens[entry_i] if has_executable_open else c[i]
        if not np.isfinite(entry_price) or entry_price <= 0:
            continue
        if has_execution_atr:
            frozen_vol = float(entry_atr_values[entry_i])
            if not np.isfinite(frozen_vol) or frozen_vol <= 0:
                continue
        else:
            frozen_vol = float(v[i])
            if not np.isfinite(frozen_vol) or frozen_vol <= 0:
                continue
        directional_dead_zone = (
            0.5 * float(frozen_vol / entry_price)
            if has_execution_atr else 0.5 * frozen_vol
        )
        end = min(len(c), entry_i + hold)

        def _net_label_return(
            gross_return: float,
            direction: str,
            exit_i: int,
            exit_price: float,
            legacy_cost_bps: float,
        ) -> float | None:
            nonlocal execution_cost_pairs_priced
            if execution_cost_context is None:
                return net_return_after_costs(
                    gross_return, turnover=1.0,
                    execution_cost_bps=legacy_cost_bps,
                )
            entry_side = "buy" if direction == "long" else "sell"
            exit_side = "sell" if direction == "long" else "buy"
            # Namespace is shared with the OOS simulator so the deterministic
            # latency draw for an equivalent order is identical in labels and returns.
            event_namespace = execution_cost_namespace
            entry_cost, entry_reason = execution_cost_for_event(
                execution_cost_context,
                bar_position=entry_i,
                turnover=1.0,
                reference_price=float(entry_price),
                side=entry_side,
                namespace=f"{event_namespace}:ENTRY",
            )
            if entry_cost is None:
                reason = entry_reason or "EXECUTION_LABEL_ENTRY_COST_UNAVAILABLE"
                execution_cost_failures[reason] = execution_cost_failures.get(reason, 0) + 1
                return None
            exit_cost, exit_reason = execution_cost_for_event(
                execution_cost_context,
                bar_position=exit_i,
                turnover=1.0,
                reference_price=float(exit_price),
                side=exit_side,
                namespace=f"{event_namespace}:EXIT",
            )
            if exit_cost is None:
                reason = exit_reason or "EXECUTION_LABEL_EXIT_COST_UNAVAILABLE"
                execution_cost_failures[reason] = execution_cost_failures.get(reason, 0) + 1
                return None
            execution_cost_pairs_priced += 1
            return net_return_after_costs(
                gross_return,
                turnover=1.0,
                execution_cost_bps=float(entry_cost) + float(exit_cost),
            )

        for j in range(entry_i, end):
            # For a session schedule, charge the entry-side rate at entry and the
            # exit-side rate at this bar. Static profile inputs are already round-trip.
            exit_cost_bps = (
                float(cost_bps[entry_i]) + float(cost_bps[j])
                if session_cost_schedule else float(cost_bps[entry_i])
            )
            if has_execution_atr:
                # Match simulate_execution_aware_strategy exactly: its stop/target
                # prices are fixed from next-open and the prior completed ATR.
                upper = entry_price + float(target_mult) * frozen_vol
                lower = entry_price - float(stop_mult) * frozen_vol
            else:
                # Fees are deducted from realized directional returns below; do not
                # hide them by moving the executable offline label barriers as well.
                upper = entry_price * (1.0 + pt_mult * frozen_vol)
                lower = entry_price * (1.0 - sl_mult * frozen_vol)
            # Price gaps are executable at the observed open, not at a stale barrier.
            if has_executable_open and j > entry_i and opens[j] >= upper:
                fill = float(opens[j])
                gross_return = fill / entry_price - 1.0
                long_net_return = _net_label_return(
                    gross_return, "long", j, fill, exit_cost_bps
                )
                y[i] = (
                    1 if long_net_return is not None and long_net_return > directional_dead_zone
                    else (0 if long_net_return is not None else np.nan)
                )
                break
            if has_executable_open and j > entry_i and opens[j] <= lower:
                fill = float(opens[j])
                gross_return = (entry_price - fill) / entry_price
                short_net_return = _net_label_return(
                    gross_return, "short", j, fill, exit_cost_bps
                )
                y[i] = (
                    -1 if short_net_return is not None and short_net_return > directional_dead_zone
                    else (0 if short_net_return is not None else np.nan)
                )
                break
            up_hit = np.isfinite(h[j]) and h[j] >= upper
            down_hit = np.isfinite(l[j]) and l[j] <= lower
            if up_hit and down_hit:
                # Match the OHLC executor's stop-first fill, but only label a
                # short when its realized return remains profitable after costs.
                fill = lower
                if has_executable_open and j > entry_i and opens[j] < lower:
                    fill = float(opens[j])
                gross_return = (entry_price - fill) / entry_price
                short_net_return = _net_label_return(
                    gross_return, "short", j, float(fill), exit_cost_bps
                )
                y[i] = (
                    -1 if short_net_return is not None and short_net_return > directional_dead_zone
                    else (0 if short_net_return is not None else np.nan)
                )
                break
            if up_hit:
                fill = float(opens[j]) if has_executable_open and j > entry_i and opens[j] > upper else float(upper)
                gross_return = fill / entry_price - 1.0
                long_net_return = _net_label_return(
                    gross_return, "long", j, fill, exit_cost_bps
                )
                y[i] = (
                    1 if long_net_return is not None and long_net_return > directional_dead_zone
                    else (0 if long_net_return is not None else np.nan)
                )
                break
            if down_hit:
                fill = float(lower)
                if has_executable_open and j > entry_i and opens[j] < lower:
                    fill = float(opens[j])
                gross_return = (entry_price - fill) / entry_price
                short_net_return = _net_label_return(
                    gross_return, "short", j, fill, exit_cost_bps
                )
                y[i] = (
                    -1 if short_net_return is not None and short_net_return > directional_dead_zone
                    else (0 if short_net_return is not None else np.nan)
                )
                break
        else:
            terminal_price = float(c[end - 1])
            terminal_return = terminal_price / entry_price - 1.0
            # Missing terminal observations remain unlabeled. Do not let one
            # incomplete future window abort the remaining label batch.
            if not np.isfinite(terminal_return):
                continue
            timeout_cost_bps = (
                float(cost_bps[entry_i]) + float(cost_bps[end - 1])
                if session_cost_schedule else float(cost_bps[entry_i])
            )
            # Evaluate each direction after its own entry+exit costs. A flat or
            # adverse-to-both trade cannot be mislabeled as a short just because
            # long net return is negative.
            long_net_return = _net_label_return(
                terminal_return, "long", end - 1, terminal_price, timeout_cost_bps
            )
            short_net_return = _net_label_return(
                -terminal_return, "short", end - 1, terminal_price, timeout_cost_bps
            )
            if (
                long_net_return is not None and short_net_return is not None
                and np.isfinite(long_net_return) and np.isfinite(short_net_return)
            ):
                y[i] = (
                    1 if long_net_return > directional_dead_zone
                    else (-1 if short_net_return > directional_dead_zone else 0)
                )

    result = pd.Series(y, index=close.index, dtype="float64")
    result.attrs["execution_cost_model_complete"] = (
        execution_cost_context is not None
        and execution_cost_pairs_priced > 0
        and not execution_cost_failures
    )
    result.attrs["execution_cost_model_status"] = (
        "VERIFIED_PER_BAR_SPREAD_VOLUME_PARTICIPATION_LATENCY"
        if result.attrs["execution_cost_model_complete"]
        else (
            "INCOMPLETE_PER_BAR_LABEL_COSTS"
            if execution_cost_context is not None
            else "STATIC_OR_SESSION_LABEL_COSTS"
        )
    )
    result.attrs["execution_cost_model_fingerprint"] = (
        execution_cost_context.get("model_fingerprint")
        if isinstance(execution_cost_context, dict) else None
    )
    result.attrs["per_bar_execution_context_sha256"] = (
        execution_cost_context.get("context_sha256")
        if isinstance(execution_cost_context, dict) else None
    )
    result.attrs["execution_cost_failure_reasons"] = execution_cost_failures
    result.attrs["execution_cost_label_event_pairs_priced"] = execution_cost_pairs_priced
    result.attrs["execution_cost_label_warmup_bars"] = first_cost_ready_bar
    return result

def _model_paths(model_path: str):
    p = Path(model_path)
    return p, p.with_suffix(p.suffix + ".meta.json")


def _aligned_log_loss(y_true, probabilities: np.ndarray, model_classes) -> float:
    """Score encoded {-1, 0, 1} labels even when a chronological train fold omits a class."""
    y = np.asarray(y_true, dtype=int)
    probs = np.asarray(probabilities, dtype=float)
    if probs.ndim != 2 or probs.shape[0] != y.shape[0] or probs.shape[1] != len(model_classes):
        raise ValueError("probability matrix does not match validation labels/model classes")
    aligned = np.full((len(probs), 3), 1e-15, dtype=float)
    for column, label in enumerate(model_classes):
        label_index = int(label)
        if 0 <= label_index < 3:
            aligned[:, label_index] = np.clip(probs[:, column], 0.0, None)
    row_sums = aligned.sum(axis=1, keepdims=True)
    if not np.isfinite(aligned).all() or not np.isfinite(row_sums).all() or np.any(row_sums <= 0):
        raise ValueError("model returned non-finite or invalid probabilities")
    aligned /= row_sums
    return float(log_loss(y, aligned, labels=[0, 1, 2]))


def train_model(df: pd.DataFrame, model_path: str, min_train: int = 800, folds: int = 5, asset: str = "crypto") -> dict:
    # Fail before touching data: unsupported assets must not inherit crypto assumptions.
    if asset not in PROFILES:
        raise ValueError(f"No validated training cost/annualization profile for asset={asset!r}")
    profile = PROFILES[asset]
    X = build_features(df)
    aligned = df.reindex(X.index)
    y = triple_barrier(
        aligned["close"], X["vol_24"], aligned["high"], aligned["low"],
        max_hold=profile.max_hold, entry_open=aligned["open"],
        round_trip_cost_bps=2.0 * (float(profile.taker_bps) + float(profile.slippage_bps)),
    )
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
            verbosity=-1, bagging_freq=1, n_jobs=settings.ml_threads,
        )
        model.fit(X.iloc[tr_idx], encoded.iloc[tr_idx].to_numpy(dtype=int))
        pred = model.predict(X.iloc[te_idx])
        probs = model.predict_proba(X.iloc[te_idx])
        scores.append({
            "balanced_accuracy": float(balanced_accuracy_score(encoded.iloc[te_idx], pred)),
            "f1_macro": float(f1_score(encoded.iloc[te_idx], pred, average="macro", zero_division=0)),
            "log_loss": _aligned_log_loss(encoded.iloc[te_idx].to_numpy(dtype=int), probs, model.classes_),
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
        verbosity=-1, bagging_freq=1, n_jobs=settings.ml_threads,
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
        "classes": [int(cls) for cls in final_model.classes_],
        "class_mapping": {"0": -1, "1": 0, "2": 1},
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
    if settings.environment in {"staging", "production"}:
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


def _research_trial_dsr_report(
    net_returns: np.ndarray,
    trial_sharpes_annual: list[float] | None,
    bars_per_year: int,
) -> dict:
    """Compute DSR only when the full experiment's trial Sharpe history is supplied.

    Fold Sharpe values are not a substitute for the distribution of all strategy,
    feature, threshold, and parameter variants explored during selection. A missing
    or incomplete trial ledger must not produce a promotion-grade DSR.
    """
    if not trial_sharpes_annual:
        return {
            "status": "MISSING_TRIAL_HISTORY",
            "observations": int(np.isfinite(np.asarray(net_returns, dtype=float)).sum()),
            "note": "Provide every evaluated variant's annualized Sharpe from the immutable experiment ledger.",
        }
    try:
        trials = [float(value) for value in trial_sharpes_annual]
    except (TypeError, ValueError, OverflowError):
        return {"status": "INVALID_TRIAL_HISTORY", "observations": int(len(net_returns))}
    if not trials or not all(np.isfinite(value) for value in trials):
        return {"status": "INVALID_TRIAL_HISTORY", "observations": int(len(net_returns))}
    return deflated_sharpe_report(
        net_returns,
        trials,
        len(trials),
        bars_per_year,
    )


def ai_walk_forward_backtest(
    df: pd.DataFrame,
    asset: str = "crypto",
    folds: int = 5,
    min_train: int = 800,
    threshold: float = 0.05,
    cost_stress_multiplier: float = 2.0,
    research_trial_sharpes: list[float] | None = None,
    bars_per_year: int | None = None,
    taker_bps: float | None = None,
    slippage_bps: float | None = None,
    carry_bps_per_bar: float | None = None,
    max_hold_bars: int | None = None,
    research_trial_evidence_verified: bool = False,
    session_costs_bps: dict[str, float] | None = None,
    execution_cost_context: dict[str, object] | None = None,
) -> dict:
    """Chronological OOS backtest using explicit annualization and execution-cost assumptions."""
    try:
        stress_multiplier = float(cost_stress_multiplier)
        profile = PROFILES[asset]
        raw_annualization = profile.bars_per_year if bars_per_year is None else bars_per_year
        raw_hold_horizon = profile.max_hold if max_hold_bars is None else max_hold_bars
        if (
            isinstance(raw_annualization, (bool, np.bool_))
            or not isinstance(raw_annualization, (int, np.integer))
            or isinstance(raw_hold_horizon, (bool, np.bool_))
            or not isinstance(raw_hold_horizon, (int, np.integer))
        ):
            raise ValueError("bars_per_year and max_hold_bars must be explicit integer values")
        annualization = int(raw_annualization)
        taker_cost_bps = float(profile.taker_bps if taker_bps is None else taker_bps)
        slippage_cost_bps = float(profile.slippage_bps if slippage_bps is None else slippage_bps)
        configured_carry = carry_bps_per_bar
        if configured_carry is None and settings.research_carry_bps_per_bar is not None:
            configured_carry = settings.research_carry_bps_per_bar
        carry_rate_configured = configured_carry is not None
        carry_evidence_source = str(settings.research_carry_evidence_source or "").strip()
        carry_evidence_id = str(settings.research_carry_evidence_id or "").strip()
        carry_evidence_complete = carry_rate_configured and bool(carry_evidence_source) and bool(carry_evidence_id)
        carry_cost_bps = float(profile.carry_bps_per_bar if configured_carry is None else configured_carry)
        hold_horizon = int(raw_hold_horizon)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid asset or walk-forward evaluation assumptions") from exc
    if not np.isfinite(stress_multiplier) or stress_multiplier <= 0:
        raise ValueError("cost_stress_multiplier must be a finite positive number")
    if annualization < 1 or hold_horizon < 1:
        raise ValueError("bars_per_year and max_hold_bars must be positive integers")
    if (
        not np.isfinite(taker_cost_bps)
        or not np.isfinite(slippage_cost_bps)
        or not np.isfinite(carry_cost_bps)
        or taker_cost_bps < 0
        or slippage_cost_bps < 0
        or carry_cost_bps < 0
    ):
        raise ValueError("Execution costs and carry must be finite non-negative bps values")
    X = build_features(df)
    aligned = df.reindex(X.index)
    from .research_execution_costs import (
        ResearchExecutionCostError,
        make_execution_cost_provider,
        slice_research_execution_context,
    )
    label_execution_context = None
    if execution_cost_context is not None:
        label_execution_context = slice_research_execution_context(
            execution_cost_context, aligned.index
        )
        if label_execution_context is None:
            raise ValueError("research execution context cannot be aligned to AI label rows")
    # Use the identical completed-bar ATR input used by the execution simulator:
    # ATR is shifted one bar so the signal at close[i] enters at open[i+1].
    label_true_range = pd.concat([
        (aligned["high"] - aligned["low"]).abs(),
        (aligned["high"] - aligned["close"].shift(1)).abs(),
        (aligned["low"] - aligned["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    entry_atr_for_label = label_true_range.rolling(14, min_periods=14).mean().shift(1)
    label_round_trip_costs: float | pd.Series = 2.0 * (taker_cost_bps + slippage_cost_bps)
    if session_costs_bps is not None:
        from .market_sessions import session_cost_series
        # The labeler combines entry-session and exit-session per-side rates for
        # each candidate exit bar; do not flatten them into a static round-trip fee.
        label_round_trip_costs = session_cost_series(aligned.index, session_costs_bps)
    y = triple_barrier(
        aligned.close, X.vol_24, aligned.high, aligned.low,
        max_hold=hold_horizon, entry_open=aligned.open,
        entry_atr=entry_atr_for_label,
        stop_atr=float(settings.strategy_stop_atr),
        take_profit_atr=float(settings.strategy_take_profit_atr),
        round_trip_cost_bps=label_round_trip_costs,
        execution_cost_context=label_execution_context,
        execution_cost_namespace="ai-walk-forward",
    )
    label_execution_cost_audit = {
        "execution_cost_model_complete": y.attrs.get("execution_cost_model_complete") is True,
        "execution_cost_model_status": y.attrs.get("execution_cost_model_status"),
        "execution_cost_failure_reasons": dict(y.attrs.get("execution_cost_failure_reasons") or {}),
        "execution_cost_label_event_pairs_priced": int(
            y.attrs.get("execution_cost_label_event_pairs_priced", 0) or 0
        ),
    }
    # Keep the original feature/price timeline intact. Labels can be unavailable
    # when signed per-bar execution pricing rejects an event; such rows are excluded
    # from model fitting only, never from the OOS OHLC timeline or next-open timing.
    valid_label_mask = y.notna()
    entry_atr_for_label = entry_atr_for_label.reindex(X.index)
    # Use expanding chronological splits on the uncompressed feature timeline.
    # The label horizon is purged in original bar units, and only valid labels within
    # each training window are eligible to fit the model.
    minimum_test_bars = 20
    gap = hold_horizon
    required_bars = int(min_train) + int(folds) * minimum_test_bars + (2 * int(folds) - 1) * gap
    if folds < 2 or min_train < 1 or len(X) < required_bars:
        raise RuntimeError("Insufficient data for requested purged AI walk-forward folds")
    test_size = (len(X) - int(min_train) - (2 * int(folds) - 1) * gap) // int(folds)
    split_specs = purged_walk_forward_splits(
        len(X),
        folds=int(folds),
        min_train=int(min_train),
        test_size=int(test_size),
        purge=gap,
        embargo=gap,
    )
    if len(split_specs) != int(folds) or any((fold.test_end - fold.test_start) < minimum_test_bars for fold in split_specs):
        raise RuntimeError("Could not construct the requested number of non-empty purged OOS folds")
    scores = []
    fold_metrics = []
    oos_net_blocks: list[np.ndarray] = []
    oos_gross_blocks: list[np.ndarray] = []
    oos_turnover_blocks: list[np.ndarray] = []
    oos_position_blocks: list[np.ndarray] = []
    oos_carry_blocks: list[np.ndarray] = []
    oos_variable_cost_blocks: list[np.ndarray] = []
    oos_execution_cost_context_blocks: list[dict[str, object]] = []
    fold_execution_cost_complete: list[bool] = []
    execution_cost_failure_reasons: dict[str, int] = dict(
        label_execution_cost_audit["execution_cost_failure_reasons"]
    )
    oos_ohlc_blocks: list[pd.DataFrame] = []
    oos_entry_signal_blocks: list[np.ndarray] = []
    oos_entry_atr_blocks: list[np.ndarray] = []
    total_stop_exits = total_target_exits = total_gap_exits = total_ambiguous_bars = 0
    pred_signal = pd.Series(np.nan, index=X.index)
    for fold in split_specs:
        train_window = np.arange(fold.train_start, fold.train_end, dtype=int)
        te = np.arange(fold.test_start, fold.test_end, dtype=int)
        # Filter labels only inside the chronological training window. Test rows
        # remain contiguous original bars, including rows whose labels were unpriced.
        tr = train_window[valid_label_mask.iloc[train_window].to_numpy(dtype=bool)]
        if len(tr) < min_train or len(np.unique((y.iloc[tr] + 1).to_numpy(dtype=int))) < 2:
            continue
        m = LGBMClassifier(objective="multiclass", class_weight="balanced",
                           n_estimators=300, max_depth=3, learning_rate=0.05,
                           subsample=0.8, colsample_bytree=0.8, min_child_samples=50,
                           reg_lambda=1.0, random_state=7, verbosity=-1, bagging_freq=1, n_jobs=settings.ml_threads)
        m.fit(X.iloc[tr], (y.iloc[tr] + 1).to_numpy(dtype=int))
        p = m.predict_proba(X.iloc[te])
        # Explicit class order from model, mapped to {-1,0,1}.
        mp = {int(cls): p[:, i] for i, cls in enumerate(m.classes_)}
        long_p = mp.get(2, np.zeros(len(te)))
        short_p = mp.get(0, np.zeros(len(te)))
        score = long_p - short_p
        sig = np.where(score >= threshold, 1, np.where(score <= -threshold, -1, 0))
        pred_signal.iloc[te] = sig
        scores.extend(score.tolist())
        # The model decision and ATR are shifted to use only the prior completed bar.
        fold_position = np.r_[0, sig[:-1]].astype(float)
        entry_atr = entry_atr_for_label
        fold_frame = aligned.iloc[te]
        fold_targets = pd.Series(fold_position, index=fold_frame.index, dtype=float)
        fold_execution_context = None
        fold_cost_provider = None
        if execution_cost_context is not None:
            fold_execution_context = slice_research_execution_context(
                execution_cost_context, fold_frame.index
            )
            if fold_execution_context is None:
                raise ValueError("research execution context cannot be aligned to an AI OOS fold")
            fold_cost_provider = make_execution_cost_provider(
                fold_execution_context,
                namespace="ai-walk-forward",
            )
        try:
            execution = simulate_execution_aware_strategy(
                fold_frame,
                fold_targets,
                entry_atr.iloc[te],
                stop_atr=float(settings.strategy_stop_atr),
                take_profit_atr=float(settings.strategy_take_profit_atr),
                transaction_cost_bps=taker_cost_bps + slippage_cost_bps,
                session_costs_bps=session_costs_bps if fold_cost_provider is None else None,
                transaction_cost_provider=fold_cost_provider,
                carry_bps_per_bar=carry_cost_bps,
                drawdown_limit=settings.strategy_drawdown_halt,
                daily_loss_limit=settings.strategy_daily_loss_halt,
                loss_event_threshold=settings.strategy_stoploss_guard_trades,
                cooldown_bars=settings.strategy_stoploss_guard_bars,
            )
            fold_cost_complete = (
                fold_cost_provider is not None
                and execution["audit"].get("per_bar_execution_cost_model_complete") is True
            )
        except ResearchExecutionCostError as exc:
            reason = str(exc)[:240] or "RESEARCH_EXECUTION_EVENT_COST_UNAVAILABLE"
            execution_cost_failure_reasons[reason] = execution_cost_failure_reasons.get(reason, 0) + 1
            # A rejected calibrated event invalidates the whole fold's calibrated cost
            # stream. Re-run the fold wholly on its configured diagnostic cost basis;
            # never mix calibrated and static costs inside a single return series.
            execution = simulate_execution_aware_strategy(
                fold_frame,
                fold_targets,
                entry_atr.iloc[te],
                stop_atr=float(settings.strategy_stop_atr),
                take_profit_atr=float(settings.strategy_take_profit_atr),
                transaction_cost_bps=taker_cost_bps + slippage_cost_bps,
                session_costs_bps=session_costs_bps,
                transaction_cost_provider=None,
                carry_bps_per_bar=carry_cost_bps,
                drawdown_limit=settings.strategy_drawdown_halt,
                daily_loss_limit=settings.strategy_daily_loss_halt,
                loss_event_threshold=settings.strategy_stoploss_guard_trades,
                cooldown_bars=settings.strategy_stoploss_guard_bars,
            )
            execution["audit"]["per_bar_execution_cost_model_complete"] = False
            execution["audit"]["per_bar_execution_cost_model_status"] = "FALLBACK_AFTER_EVENT_REJECTION"
            execution["audit"]["per_bar_execution_cost_failure"] = reason
            fold_cost_complete = False
        fold_execution_cost_complete.append(fold_cost_complete)
        if execution_cost_context is not None:
            # Preserve the actual per-fold execution basis: successful folds retain
            # their signed context, while a rejected fold is benchmarked on the same
            # static/session fallback used for its reported candidate returns.
            oos_execution_cost_context_blocks.append(
                fold_execution_context if fold_cost_complete else None
            )
        fold_position = execution["positions"].to_numpy(dtype=float)
        total_stop_exits += int(execution["audit"]["stop_loss_exits"])
        total_target_exits += int(execution["audit"]["take_profit_exits"])
        total_gap_exits += int(execution["audit"]["gap_exits"])
        total_ambiguous_bars += int(execution["audit"]["ambiguous_bars_filled_stop_first"])
        fold_gross = fold_position * execution["market_returns"].to_numpy(dtype=float)
        fold_turnover = execution["turnover"].to_numpy(dtype=float)
        fold_carry = np.abs(fold_position) * (carry_cost_bps / 10_000.0)
        fold_net = execution["net_returns"].to_numpy(dtype=float)
        oos_net_blocks.append(np.asarray(fold_net, dtype=float))
        oos_gross_blocks.append(np.asarray(fold_gross, dtype=float))
        oos_turnover_blocks.append(np.asarray(fold_turnover, dtype=float))
        oos_position_blocks.append(np.asarray(fold_position, dtype=float))
        oos_carry_blocks.append(np.asarray(fold_carry, dtype=float))
        if session_costs_bps is not None or execution_cost_context is not None:
            oos_variable_cost_blocks.append(
                execution["effective_transaction_cost_bps"].to_numpy(dtype=float)
            )
        # Preserve raw OHLC and the requested entry stream before stops or risk halts
        # condition realized positions and returns. Benchmark controls resimulate both.
        oos_ohlc_blocks.append(fold_frame.copy())
        oos_entry_signal_blocks.append(fold_targets.to_numpy(dtype=float))
        oos_entry_atr_blocks.append(entry_atr.iloc[te].to_numpy(dtype=float))
        fold_equity = np.cumprod(1.0 + fold_net) if len(fold_net) else np.array([1.0])
        fold_peak = np.maximum.accumulate(fold_equity)
        fold_dd = fold_equity / fold_peak - 1.0
        fold_sharpe = float(fold_net.mean() / fold_net.std() * np.sqrt(annualization)) if float(fold_net.std()) > 0 else 0.0
        fold_metrics.append({
            "train_bars": int(len(tr)), "test_bars": int(len(te)),
            "test_start": X.index[int(fold.test_start)].isoformat(),
            "test_end": X.index[int(fold.test_end) - 1].isoformat(),
            "purge_bars": int(fold.purge), "embargo_bars": int(fold.embargo),
            "total_return": float(fold_equity[-1] - 1.0),
            "max_drawdown": float(fold_dd.min()),
            "trades": int(np.count_nonzero(fold_turnover)), "sharpe": fold_sharpe,
            "execution_cost_model_complete": bool(fold_cost_complete),
            "execution_cost_model_status": execution["audit"].get(
                "per_bar_execution_cost_model_status"
            ),
        })
    if not fold_metrics or not oos_net_blocks:
        raise RuntimeError("No valid chronological out-of-sample folds could be evaluated")
    if len(fold_metrics) != int(folds):
        raise RuntimeError(
            f"Expected {int(folds)} purged OOS folds, but only {len(fold_metrics)} had sufficient training-class diversity"
        )
    # All aggregate statistics are calculated from held-out test windows only. Do not
    # include training rows or bars in the gaps between test folds, and do not shift a
    # final test signal into the following non-test window.
    net = pd.Series(np.concatenate(oos_net_blocks), dtype=float)
    gross_returns = np.concatenate(oos_gross_blocks)
    turnover = pd.Series(np.concatenate(oos_turnover_blocks), dtype=float)
    position = pd.Series(np.concatenate(oos_position_blocks), dtype=float)
    carry = np.concatenate(oos_carry_blocks)
    variable_execution_costs = (
        np.concatenate(oos_variable_cost_blocks) if oos_variable_cost_blocks else None
    )
    equity = (1 + net).cumprod()
    peak = equity.cummax()
    dd = equity / peak - 1
    cost_report = cost_breakeven_report(
        gross_returns,
        turnover.to_numpy(),
        taker_bps=taker_cost_bps,
        slippage_bps=slippage_cost_bps,
        bars_per_year=annualization,
        stress_multiplier=cost_stress_multiplier,
        fixed_cost_drag=carry,
        variable_cost_bps_per_bar=variable_execution_costs,
    )
    dsr_report = _research_trial_dsr_report(
        net.to_numpy(),
        research_trial_sharpes,
        annualization,
    )
    vol = float(net.std())
    annual = annualization
    downside = net[net < 0].std()
    sortino = float(net.mean() / downside * np.sqrt(annual)) if downside and np.isfinite(downside) and downside > 0 else 0.0
    max_dd = float(dd.min())
    calmar = float((equity.iloc[-1] - 1) / abs(max_dd)) if max_dd < 0 else 0.0
    wins = net[position != 0][net[position != 0] > 0].sum()
    losses = -net[position != 0][net[position != 0] < 0].sum()
    profit_factor = float(wins / losses) if losses > 0 else (float("inf") if wins > 0 else 0.0)
    inference_report = newey_west_mean_inference(net.to_numpy(dtype=float))
    benchmark_report = oos_benchmark_comparison(
        oos_ohlc_blocks,
        oos_entry_signal_blocks,
        oos_net_blocks,
        oos_entry_atr_blocks,
        transaction_cost_bps=taker_cost_bps + slippage_cost_bps,
        carry_bps_per_bar=carry_cost_bps,
        stop_atr=float(settings.strategy_stop_atr),
        take_profit_atr=float(settings.strategy_take_profit_atr),
        drawdown_limit=settings.strategy_drawdown_halt,
        daily_loss_limit=settings.strategy_daily_loss_halt,
        loss_event_threshold=settings.strategy_stoploss_guard_trades,
        cooldown_bars=settings.strategy_stoploss_guard_bars,
        seed=20261026,
        session_costs_bps=(
            session_costs_bps
            if execution_cost_context is None or not all(fold_execution_cost_complete)
            else None
        ),
        execution_cost_context_blocks=(
            oos_execution_cost_context_blocks
            if execution_cost_context is not None
            and len(oos_execution_cost_context_blocks) == len(fold_metrics)
            else None
        ),
        candidate_execution_cost_namespace="ai-walk-forward",
    )
    label_cost_complete = label_execution_cost_audit.get(
        "execution_cost_model_complete"
    ) is True
    fold_costs_complete = (
        execution_cost_context is not None
        and bool(fold_execution_cost_complete)
        and all(fold_execution_cost_complete)
    )
    benchmark_costs_complete = (
        benchmark_report.get("execution_cost_model_complete") is True
    )
    execution_cost_model_complete = (
        execution_cost_context is not None
        and label_cost_complete
        and fold_costs_complete
        and benchmark_costs_complete
    )
    if execution_cost_context is None:
        execution_cost_model_status = "STATIC_BPS_NOT_VOLUME_VOLATILITY_CALIBRATED"
    elif not label_cost_complete:
        execution_cost_model_status = "INCOMPLETE_AI_LABEL_EXECUTION_COSTS"
    elif not fold_costs_complete:
        execution_cost_model_status = "INCOMPLETE_AI_OOS_FOLD_EXECUTION_COSTS"
    elif not benchmark_costs_complete:
        execution_cost_model_status = "INCOMPLETE_AI_BENCHMARK_EXECUTION_COSTS"
    else:
        execution_cost_model_status = "VERIFIED_PER_BAR_SPREAD_VOLUME_PARTICIPATION_LATENCY"
    execution_cost_model_fingerprint = (
        str(execution_cost_context.get("model_fingerprint") or "")
        if isinstance(execution_cost_context, dict) else None
    )
    per_bar_execution_context_sha256 = (
        str(execution_cost_context.get("context_sha256") or "")
        if isinstance(execution_cost_context, dict) else None
    )
    return {
        "bars": int(len(df)), "oos_bars": int(len(net)), "features": int(X.shape[1]), "validated_bars": int(pred_signal.notna().sum()),
        "total_return": float(equity.iloc[-1] - 1),
        "max_drawdown": max_dd,
        "sharpe": float(net.mean() / vol * np.sqrt(annual)) if vol > 0 else 0.0,
        "sortino": sortino,
        "calmar": calmar,
        "profit_factor": profit_factor,
        "trades": int((turnover > 0).sum()),
        "hit_rate": float((net[position != 0] > 0).mean()) if bool((position != 0).any()) else 0.0,
        "average_active_bar_return": float(net[position != 0].mean()) if bool((position != 0).any()) else 0.0,
        "validation_signal_count": len(scores),
        "prediction_gap_policy": "NO_FORWARD_FILL_ACROSS_NON_TEST_WINDOWS",
        "inference": inference_report,
        "benchmark_comparison": benchmark_report,
        # Each model fold is simulated from flat and terminally liquidated within
        # that fold; no position is carried across the evaluated fold boundary.
        "trade_boundary_overlap_bars": 0,
        "folds": fold_metrics,
        "cost_stress_ok": bool(cost_report.get("stress_ok")) and carry_evidence_complete if cost_report.get("status") == "OK" else False,
        "session_cost_model_complete": (
            execution_cost_context is None
            and session_costs_bps is not None
            and variable_execution_costs is not None
        ),
        "session_costs_bps": (
            dict(session_costs_bps)
            if execution_cost_context is None and variable_execution_costs is not None
            else None
        ),
        "execution_cost_model_complete": bool(execution_cost_model_complete),
        "execution_cost_model_status": execution_cost_model_status,
        "execution_cost_model_fingerprint": execution_cost_model_fingerprint,
        "per_bar_execution_context_sha256": per_bar_execution_context_sha256,
        "execution_cost_calibration_status": (
            "VERIFIED" if execution_cost_context is not None else "MISSING_VENUE_CALIBRATION_EVIDENCE"
        ),
        "execution_cost_calibration_evidence_id": (
            execution_cost_context.get("evidence_id")
            if isinstance(execution_cost_context, dict) else None
        ),
        "execution_cost_failure_reasons": execution_cost_failure_reasons,
        "slippage_model_complete": bool(execution_cost_model_complete),
        "slippage_model_status": execution_cost_model_status,
        "cost_stress_multiplier": float(stress_multiplier),
        "carry_rate_configured": bool(carry_rate_configured),
        "carry_evidence_source": carry_evidence_source or None,
        "carry_evidence_id": carry_evidence_id or None,
        "financing_cost_model_complete": bool(carry_evidence_complete),
        # OHLC stop-first handling is a conservative approximation, not tick-level
        # event-order evidence. Do not let this backtest certify intrabar completeness.
        "intrabar_stop_target_model_complete": False,
        "execution_model": "OHLC_INTRABAR_ATR_STOP_TAKE_PROFIT_NEXT_OPEN",
        "execution_stop_target_audit": {
            "stop_loss_exits": int(total_stop_exits),
            "take_profit_exits": int(total_target_exits),
            "gap_exits": int(total_gap_exits),
            "ambiguous_bars_filled_stop_first": int(total_ambiguous_bars),
            "carry_bps_per_bar": carry_cost_bps,
            "carry_rate_configured": bool(carry_rate_configured),
            "carry_evidence_source": carry_evidence_source or None,
            "carry_evidence_id": carry_evidence_id or None,
            "carry_evidence_complete": bool(carry_evidence_complete),
        },
        "research_assumptions": {
            "asset_profile": asset,
            "bars_per_year": annualization,
            "taker_bps": taker_cost_bps,
            "slippage_bps": slippage_cost_bps,
            "carry_bps_per_bar": carry_cost_bps,
            "carry_rate_configured": bool(carry_rate_configured),
            "carry_evidence_source": carry_evidence_source or None,
            "carry_evidence_id": carry_evidence_id or None,
            "carry_evidence_complete": bool(carry_evidence_complete),
            "max_hold_bars": hold_horizon,
            "stop_atr": float(settings.strategy_stop_atr),
            "take_profit_atr": float(settings.strategy_take_profit_atr),
        },
        "cost_analysis": cost_report,
        "research_trial_count": len(research_trial_sharpes) if research_trial_sharpes else 0,
        "trial_registry_complete": (
            research_trial_evidence_verified is True
            and bool(research_trial_sharpes)
            and dsr_report.get("status") == "OK"
        ),
        "trial_registry_evidence_verified": research_trial_evidence_verified is True,
        "research_trial_history_status": dsr_report.get("status"),
        "deflated_sharpe": dsr_report.get("deflated_sharpe_ratio") if dsr_report.get("status") == "OK" else None,
        "deflated_sharpe_status": dsr_report.get("status"),
    }
