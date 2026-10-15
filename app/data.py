from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import numpy as np
import pandas as pd


def _validate_ohlcv(df: pd.DataFrame, timeframe: str, reject_gaps: bool = True) -> pd.DataFrame:
    required = ["open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"Missing OHLCV columns: {missing}")
    out = df[required].copy()
    out.index = pd.to_datetime(out.index, utc=True)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.replace([np.inf, -np.inf], np.nan).dropna()
    if out.empty:
        raise RuntimeError("Market data is empty after cleaning")
    bad = (
        (out["open"] <= 0) | (out["high"] <= 0) | (out["low"] <= 0) | (out["close"] <= 0)
        | (out["high"] < out[["open", "close", "low"]].max(axis=1))
        | (out["low"] > out[["open", "close", "high"]].min(axis=1))
        | (out["volume"] < 0)
    )
    if bool(bad.any()):
        raise RuntimeError("Invalid OHLCV rows detected")
    if len(out) >= 3:
        try:
            tf_ms = int(pd.Timedelta(timeframe) / pd.Timedelta(milliseconds=1))
        except Exception:
            tf_ms = None
        if tf_ms:
            delta_ms = out.index.to_series().diff().dt.total_seconds().mul(1000)
            # Gaps can legitimately occur in FX weekends, so we only reject very large gaps
            # for an aggressively misconfigured crypto stream upstream.
            if reject_gaps and (delta_ms.dropna() > tf_ms * 3).sum() > max(1, len(out) // 100):
                raise RuntimeError("Market data contains excessive timestamp gaps")
    return out


def _attach_data_provenance(df: pd.DataFrame, *, source: str, symbol: str, exchange: str,
                            timeframe: str, fetched_at: str) -> pd.DataFrame:
    """Attach a deterministic hash and source metadata to the exact cleaned frame."""
    canonical = df.reset_index().to_csv(index=False, float_format="%.17g", lineterminator="\n")
    df.attrs["data_provenance"] = {
        "source": source,
        "symbol": symbol,
        "exchange": exchange,
        "timeframe": timeframe,
        "fetched_at_utc": fetched_at,
        "row_count": int(len(df)),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    return df


def fetch_crypto(symbol: str, exchange: str = "bybit", timeframe: str = "1h", days: int = 365,
                 closed_only: bool = True) -> pd.DataFrame:
    import ccxt
    if exchange not in ccxt.exchanges:
        raise RuntimeError(f"Unsupported CCXT exchange: {exchange}")
    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    try:
        ex.load_markets()
        if symbol not in ex.markets:
            raise RuntimeError(f"Unknown symbol for {exchange}: {symbol}")
        now_ms = ex.milliseconds()
        tf_ms = ex.parse_timeframe(timeframe) * 1000
        since = now_ms - days * 86_400_000
        rows: list[list[float]] = []
        cursor = since
        while cursor < now_ms:
            batch = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            next_cursor = int(batch[-1][0]) + tf_ms
            if next_cursor <= cursor:
                raise RuntimeError("Exchange returned non-advancing OHLCV timestamps")
            cursor = next_cursor
            if len(batch) < 1000:
                break
        if not rows:
            raise RuntimeError("No crypto OHLCV returned")
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.drop_duplicates("ts").set_index("ts").sort_index()
        if closed_only:
            cutoff = pd.Timestamp(datetime.fromtimestamp((now_ms - tf_ms) / 1000, tz=timezone.utc))
            df = df[df.index <= cutoff]
        cleaned = _validate_ohlcv(df, timeframe)
        return _attach_data_provenance(
            cleaned,
            source="ccxt_ohlcv",
            symbol=symbol,
            exchange=exchange,
            timeframe=timeframe,
            fetched_at=datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat(),
        )
    finally:
        if hasattr(ex, "close") and callable(ex.close):
            ex.close()


def fetch_forex(symbol: str = "EURUSD", timeframe: str = "1h", days: int = 365) -> pd.DataFrame:
    import yfinance as yf
    yf_sym = symbol if "=X" in symbol else symbol + "=X"
    interval = timeframe if timeframe in {"1m", "5m", "15m", "30m", "1h", "1d"} else "1h"
    period = f"{min(days, 720)}d" if interval != "1d" else f"{days}d"
    raw = yf.download(yf_sym, period=period, interval=interval, auto_adjust=False, progress=False)
    if raw.empty:
        raise RuntimeError("No FX data returned")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [str(c[0]).lower() for c in raw.columns]
    else:
        raw.columns = [str(c).lower() for c in raw.columns]
    raw = raw[["open", "high", "low", "close", "volume"]].dropna()
    raw.index = pd.to_datetime(raw.index, utc=True)
    # Exclude a still-forming yfinance candle to prevent signals repainting.
    interval_seconds = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "1d": 86400}.get(interval, 3600)
    now = pd.Timestamp.now(tz="UTC")
    raw = raw.loc[(raw.index + pd.to_timedelta(interval_seconds, unit="s")) <= now]
    if raw.empty:
        raise RuntimeError("No completed FX candles returned after excluding the forming bar")
    cleaned = _validate_ohlcv(raw, timeframe, reject_gaps=False)
    return _attach_data_provenance(
        cleaned,
        source="yfinance",
        symbol=symbol,
        exchange="yfinance",
        timeframe=timeframe,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def fetch_forex_oanda(symbol: str = "EUR_USD", timeframe: str = "1h", days: int = 365) -> pd.DataFrame:
    """Fetch completed OANDA v20 mid candles for the configured practice/demo account only."""
    from .config import settings
    from .forex_oanda import OandaBroker, OandaConfig
    granularity = {"1m":"M1", "5m":"M5", "15m":"M15", "30m":"M30", "1h":"H1", "4h":"H4", "1d":"D"}.get(timeframe)
    if not granularity:
        raise RuntimeError(f"Unsupported OANDA timeframe: {timeframe}")
    if not settings.oanda_practice:
        raise RuntimeError("OANDA live mode is disabled in AtlasRisk; use practice/demo for learning and backtesting")
    broker = OandaBroker(OandaConfig(settings.oanda_account_id, settings.oanda_api_token, True, settings.oanda_timeout_seconds))
    try:
        if isinstance(days, bool) or int(days) < 1:
            raise ValueError("OANDA historical window days must be a positive integer")
        days = int(days)
        bars_per_day = {"M1": 1440, "M5": 288, "M15": 96, "M30": 48, "H1": 24, "H4": 6, "D": 1}[granularity]
        page_size = 5000
        expected_upper_bound = days * bars_per_day
        max_pages = max(1, int(np.ceil(expected_upper_bound / page_size)) + 2)
        end_time = datetime.now(timezone.utc)
        cursor = end_time - pd.Timedelta(days=days)
        candles = []
        seen_timestamps: set[pd.Timestamp] = set()
        granularity_delta = {
            "M1": pd.Timedelta(minutes=1), "M5": pd.Timedelta(minutes=5),
            "M15": pd.Timedelta(minutes=15), "M30": pd.Timedelta(minutes=30),
            "H1": pd.Timedelta(hours=1), "H4": pd.Timedelta(hours=4),
            "D": pd.Timedelta(days=1),
        }[granularity]
        for page_number in range(max_pages):
            batch = broker.candles(
                symbol,
                granularity,
                count=page_size,
                from_time=cursor.isoformat(),
                to_time=end_time.isoformat(),
            )
            if not batch:
                break
            timestamps = []
            for item in batch:
                if not isinstance(item, dict) or not item.get("time"):
                    raise RuntimeError("OANDA candle page contained a malformed timestamp row")
                timestamp = pd.Timestamp(item["time"])
                if timestamp.tzinfo is None:
                    timestamp = timestamp.tz_localize("UTC")
                else:
                    timestamp = timestamp.tz_convert("UTC")
                if timestamp in seen_timestamps:
                    raise RuntimeError(f"OANDA candle pagination returned duplicate timestamp {timestamp.isoformat()}")
                seen_timestamps.add(timestamp)
                timestamps.append(timestamp)
            if timestamps != sorted(timestamps):
                raise RuntimeError("OANDA candle page is not in chronological order")
            candles.extend(batch)
            last_timestamp = timestamps[-1]
            next_cursor = last_timestamp + granularity_delta
            if next_cursor <= cursor:
                raise RuntimeError("OANDA candle pagination cursor did not advance")
            cursor = next_cursor
            if len(batch) < page_size or cursor >= end_time:
                break
        else:
            raise RuntimeError(f"OANDA candle pagination exceeded safety bound of {max_pages} pages")
    finally:
        broker.close()
    rows = []
    for c in candles:
        if not c.get("complete"):
            continue
        m = c.get("mid") or {}
        try:
            rows.append([c.get("time"), float(m["o"]), float(m["h"]), float(m["l"]), float(m["c"]), float(c.get("volume") or 0)])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("OANDA returned a completed candle with malformed mid OHLCV values") from exc
    if not rows:
        raise RuntimeError("No completed OANDA candles returned for requested time window")
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    cleaned = _validate_ohlcv(df.set_index("ts"), timeframe, reject_gaps=False)
    return _attach_data_provenance(
        cleaned,
        source="oanda_v20_mid",
        symbol=symbol,
        exchange="oanda",
        timeframe=timeframe,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
