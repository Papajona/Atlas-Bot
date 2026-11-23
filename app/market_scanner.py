"""Bounded fan-out helpers for the customer market scanner.

The endpoint used to start one worker thread per requested symbol and every thread
built its own CCXT client and called load_markets(). This could occupy most of the
process-wide default thread pool shared by other asyncio.to_thread callers. These
helpers preserve the response shape while limiting symbol count and concurrency.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any

MAX_SYMBOLS = 8
MAX_SYMBOL_LENGTH = 40
MAX_CONCURRENT_FETCHES = 4
GENERIC_ERROR = "Market data unavailable"


def normalize_symbols(raw: str | Iterable[str], limit: int = MAX_SYMBOLS) -> list[str]:
    """Split, trim, de-duplicate (order preserved), and cap requested symbols."""
    parts = raw.split(",") if isinstance(raw, str) else list(raw)
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        symbol = str(part).strip()
        if not symbol or len(symbol) > MAX_SYMBOL_LENGTH or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
        if len(out) >= limit:
            break
    return out


async def scan_symbols(
    symbols: list[str],
    *,
    fetch: Callable[[str], Any],
    metrics: Callable[[Any], dict],
    semaphore: asyncio.Semaphore,
    logger: Any = None,
) -> list[dict]:
    """Fetch each symbol in a worker thread with bounded concurrency.

    A failing symbol yields a generic error response. The underlying exception is
    logged server-side and is never returned to the customer.
    """

    async def one(symbol: str) -> dict:
        try:
            async with semaphore:
                frame = await asyncio.to_thread(fetch, symbol)
            return {"symbol": symbol, **metrics(frame)}
        except Exception:
            if logger is not None:
                logger.warning(
                    "market_scanner_symbol_failed symbol=%s",
                    symbol,
                    exc_info=True,
                )
            return {
                "symbol": symbol,
                "signal": "ERROR",
                "error": GENERIC_ERROR,
            }

    return list(await asyncio.gather(*(one(symbol) for symbol in symbols)))
