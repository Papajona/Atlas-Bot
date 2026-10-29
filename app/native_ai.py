"""Atlas Native AI: constrained local strategy composition and deterministic review.

No hosted model, model API key, external inference endpoint or network call is used.
This is not a general-purpose generative language model. Statistical prediction continues
to use Atlas's existing locally executed adaptive ML pipeline.
"""
from __future__ import annotations

import math
import re
from typing import Any


class NativeAIError(RuntimeError):
    """Raised when the local Native AI engine cannot produce a valid draft."""


AIProviderError = NativeAIError  # compatibility alias; no provider is called
_ALLOWED_TIMEFRAMES = ("5m", "15m", "30m", "1h", "4h", "1d")
_ALLOWED_ASSETS = ("crypto", "forex", "commodity")
_ALLOWED_FAMILIES = ("TREND", "MOMENTUM", "BREAKOUT", "MEAN_REVERSION", "ENSEMBLE")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _classify_asset(text: str, requested: str | None) -> str:
    if requested in _ALLOWED_ASSETS:
        return requested
    value = text.lower()
    if any(x in value for x in ("forex", "eurusd", "eur_usd", "gbpusd", "gbp_usd", "usdjpy", "usd_jpy", "currency")):
        return "forex"
    if any(x in value for x in ("commodity", "gold", "xau", "wti", "brent", "crude oil", "natural gas", "silver", "xag")):
        return "commodity"
    return "crypto"


def _classify_family(text: str) -> str:
    value = text.lower()
    # Explicit multi-strategy intent takes precedence over component keywords.
    if any(x in value for x in ("ensemble", "combine strategies", "multiple strategies", "combine momentum and trend")):
        return "ENSEMBLE"
    if any(x in value for x in ("mean reversion", "mean-reversion", "revert to mean", "range-bound", "oversold", "overbought")):
        return "MEAN_REVERSION"
    if any(x in value for x in ("breakout", "break out", "prior high", "range high", "range low")):
        return "BREAKOUT"
    if any(x in value for x in ("momentum", "rate of change", "acceleration")) or re.search(r"\broc\b", value):
        return "MOMENTUM"
    return "TREND"


def _template_rules(family: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    entries = {
        "TREND": [
            {"type": "TREND_CONFIRMATION", "rule": "Require completed-bar agreement between configured fast and slow trend filters."},
            {"type": "MOMENTUM_CONFIRMATION", "rule": "Require momentum confirmation; never enter from an unfinished candle."}],
        "MOMENTUM": [
            {"type": "MOMENTUM_CONFIRMATION", "rule": "Require momentum direction to agree with the proposed side on completed bars."},
            {"type": "VOLATILITY_FILTER", "rule": "Reject when volatility, spread or data quality exceeds configured limits."}],
        "BREAKOUT": [
            {"type": "RANGE_BREAKOUT", "rule": "Require a completed-bar close beyond a prior rolling range boundary."},
            {"type": "BREAKOUT_QUALITY", "rule": "Require configured confirmation and reject stale or incomplete data."}],
        "MEAN_REVERSION": [
            {"type": "EXTREME_DEVIATION", "rule": "Require measured deviation from rolling mean/VWAP and reversal confirmation."},
            {"type": "RANGE_REGIME", "rule": "Reject when trend strength conflicts with a mean-reversion regime."}],
        "ENSEMBLE": [
            {"type": "SIGNAL_CONSENSUS", "rule": "Require at least two supported Atlas strategy components to agree on completed bars."},
            {"type": "SIGNAL_QUALITY", "rule": "Reject when signal quality, coverage or spread gates fail."}],
    }[family]
    exits = {
        "TREND": "Exit if trend conditions invalidate or a protective stop/target is reached.",
        "MOMENTUM": "Exit on momentum reversal or protective stop/target.",
        "BREAKOUT": "Exit if price closes back inside the prior range or a protective stop/target is reached.",
        "MEAN_REVERSION": "Exit at the configured mean/return threshold or protective stop/target.",
        "ENSEMBLE": "Exit when component consensus breaks or a protective stop/target is reached.",
    }
    return entries, [{"type": f"{family}_EXIT", "rule": exits[family]}]


def _condition_timeframe(prompt: str, condition_type: str, default_timeframe: str) -> str:
    """Bind supported rules to the nearest explicitly requested timeframe when clear."""
    prompt_lower = prompt.lower()
    timeframe_matches = list(re.finditer(
        r"(?<![a-z0-9])(5m|15m|30m|1h|4h|1d)(?![a-z0-9])", prompt_lower
    ))
    if len(timeframe_matches) < 2:
        return default_timeframe
    keywords = {
        "RANGE_BREAKOUT": ("breakout", "break out", "prior high", "range high", "range low"),
        "BREAKOUT_QUALITY": ("confirmation", "confirm", "quality"),
        "TREND_CONFIRMATION": ("trend", "fast", "slow", "confirmation", "confirm"),
        "MOMENTUM_CONFIRMATION": ("momentum", "confirmation", "confirm"),
        "VOLATILITY_FILTER": ("volatility", "spread", "data quality"),
        "EXTREME_DEVIATION": ("mean", "deviation", "vwap", "reversal"),
        "RANGE_REGIME": ("regime", "range", "trend"),
        "SIGNAL_CONSENSUS": ("consensus", "ensemble", "components", "strategies"),
        "SIGNAL_QUALITY": ("quality", "coverage", "spread"),
        "BREAKOUT_EXIT": ("breakout", "exit", "range"),
    }.get(condition_type, ())
    keyword_spans = []
    for keyword in keywords:
        for match in re.finditer(re.escape(keyword), prompt_lower):
            keyword_spans.append((match.start(), match.end()))
    if not keyword_spans:
        return default_timeframe
    ranked = []
    for timeframe_match in timeframe_matches:
        distance = min(
            max(0, max(start - timeframe_match.end(), timeframe_match.start() - end))
            for start, end in keyword_spans
        )
        ranked.append((distance, timeframe_match.group(1)))
    distance, selected = min(ranked, key=lambda item: item[0])
    # Do not bind a rule to a distant timeframe mention without local textual evidence.
    return selected if distance <= 48 else default_timeframe


def compose_strategy_spec(*, prompt: str, name: str, asset: str | None = None,
                          symbol: str | None = None, timeframe: str | None = None,
                          max_risk_per_trade: float = 0.005) -> dict[str, Any]:
    """Map a request to a supported strategy template, not to generated executable code."""
    text = str(prompt or "").strip()
    if not text:
        raise NativeAIError("Enter a strategy request before generating a draft.")
    title = re.sub(r"\s+", " ", str(name or "Atlas Native Strategy").strip())[:120] or "Atlas Native Strategy"
    asset_kind = _classify_asset(f"{text} {symbol or ''}", asset)
    if symbol and symbol.strip():
        selected_symbol = symbol.strip()[:80]
    else:
        upper = text.upper()
        match = next(((pattern, label) for pattern, label in (
            (r"\bEUR\s*[/_-]?\s*USD\b", "EUR_USD"),
            (r"\bGBP\s*[/_-]?\s*USD\b", "GBP_USD"),
            (r"\bXAU(?:\s*[/_-]?\s*USD)?\b|\bGOLD\b", "XAU_USD"),
            (r"\bBTC(?:\s*[/_-]?\s*USDT?)?\b", "BTC/USDT"),
            (r"\bETH(?:\s*[/_-]?\s*USDT?)?\b", "ETH/USDT"),
            (r"\bWTI\b|\bCRUDE\s*OIL\b", "WTI"),
        ) if re.search(pattern, upper)), None)
        selected_symbol = match[1] if match else {"crypto": "BTC/USDT", "forex": "EUR_USD", "commodity": "XAU_USD"}[asset_kind]
    requested_timeframes = list(dict.fromkeys(
        match.group(1) for match in re.finditer(
            r"(?<![a-z0-9])(5m|15m|30m|1h|4h|1d)(?![a-z0-9])", text.lower()
        )
    ))
    tf = timeframe if timeframe in _ALLOWED_TIMEFRAMES else (requested_timeframes[0] if requested_timeframes else "1h")
    if tf not in requested_timeframes:
        requested_timeframes.append(tf)
    family = _classify_family(text)
    lower = text.lower()
    asks_both = any(x in lower for x in ("both directions", "long and short", "long/short", "both sides"))
    # "short EMA" and "long EMA" describe indicator periods, while "sell when RSI"
    # describes an exit. Only explicit direction phrases or a known instrument imply side.
    known_symbol = (
        r"(?:btc(?:\s*[/_-]?\s*usdt?)?|bitcoin|eth(?:\s*[/_-]?\s*usdt?)?|ethereum|"
        r"eur\s*[/_-]?\s*usd|gbp\s*[/_-]?\s*usd|xau(?:\s*[/_-]?\s*usd)?|gold|wti|crude\s+oil)"
    )
    asks_short = bool(
        re.search(r"\b(?:bearish|downtrend|shorting|go\s+short|short\s+only|short[-\s]sell)\b", lower)
        or re.search(rf"\bshort\s+(?:the\s+)?{known_symbol}\b", lower)
        or re.search(r"\bsell\s+only\b", lower)
        or re.search(rf"\bsell\s+(?:the\s+)?{known_symbol}\b", lower)
    )
    asks_long = bool(
        re.search(r"\b(?:buy|bullish|uptrend)\b", lower)
        or re.search(r"\blong\b(?!\s+(?:ema|sma|ma|moving\s+average|window|period|lookback|average)\b)", lower)
    )
    direction = "BOTH" if asks_both or (asks_short and asks_long) else ("SHORT" if asks_short else "LONG")
    direction_ambiguous = not (asks_both or asks_short or asks_long)
    cap = _number(max_risk_per_trade)
    cap = min(cap if cap is not None and cap > 0 else 0.005, 0.05)
    risk = min(cap, 0.005)
    warnings = [
        "Generated locally by Atlas Native AI from supported templates; not a general-purpose language-model response.",
        "This draft is not backtest evidence; cost-aware out-of-sample and walk-forward validation remain mandatory before paper promotion."]
    if direction_ambiguous:
        warnings.append("No trading direction was explicit in the prompt; LONG is only a draft default and must be confirmed before use.")
    risk_match = re.search(r"\brisk(?:\s+per\s+trade)?\s*(?:of|=|:)?\s*(\d+(?:\.\d+)?)\s*(%|percent|bps|bp)?", lower)
    if risk_match:
        amount = float(risk_match.group(1))
        unit = (risk_match.group(2) or "").lower()
        requested = amount / (10000.0 if unit in {"bp", "bps"} else 100.0 if unit in {"%", "percent"} or amount > 0.05 else 1.0)
        if math.isfinite(requested) and requested > 0:
            risk = min(cap, requested)
            if requested > cap:
                warnings.append(f"Requested risk capped at platform maximum {cap:.4f}.")
    atr_match = re.search(r"(\d+(?:\.\d+)?)\s*atr", lower)
    stop_atr = min(10.0, max(0.1, float(atr_match.group(1)))) if atr_match else 1.5
    rr_match = re.search(r"(?:take.?profit|reward.?risk|risk.?reward|target)\s*(?:of|=|:)?\s*(\d+(?:\.\d+)?)\s*r\b", lower)
    target_rr = min(10.0, max(0.1, float(rr_match.group(1)))) if rr_match else 2.0
    entries, exits = _template_rules(family)
    entry_conditions = [dict(x, timeframe=_condition_timeframe(text, x["type"], tf)) for x in entries]
    exit_conditions = [dict(x, timeframe=_condition_timeframe(text, x["type"], tf)) for x in exits]
    sessions = [n for n, term in (("ASIA", "asia"), ("LONDON", "london"), ("NEW_YORK", "new york"), ("SYDNEY", "sydney")) if term in lower]
    return {
        "version": "atlas-strategy-v1", "title": title,
        "summary": f"{family.replace('_', ' ').title()} template for {selected_symbol} on {tf}; empirically validate all parameters.",
        "asset": asset_kind, "symbols": [selected_symbol], "timeframes": requested_timeframes, "direction": direction,
        "strategy_family": family, "entry_conditions": entry_conditions,
        "exit_conditions": exit_conditions, "risk_per_trade": risk,
        "stop_loss_atr": stop_atr, "take_profit_rr": target_rr, "sessions": sessions,
        "assumptions": ["Use completed candles only.", "Use verified market data and realistic costs.", "Platform risk controls override this draft."],
        "validation_warnings": warnings, "requires_backtest": True, "requires_walk_forward": True,
        "execution_mode": "PAPER_SHADOW_ONLY", "implementation_status": "ATLAS_SUPPORTED_FAMILY"}


def native_research_review(research: dict[str, Any], intelligence: dict[str, Any]) -> dict[str, Any]:
    """Summarize computed evidence; report gaps and never authorize promotion."""
    research = research if isinstance(research, dict) else {}
    intelligence = intelligence if isinstance(intelligence, dict) else {}
    regime = research.get("regime") if isinstance(research.get("regime"), dict) else {}
    validation = research.get("validation") if isinstance(research.get("validation"), dict) else {}
    oos = validation.get("per_strategy_oos") if isinstance(validation.get("per_strategy_oos"), dict) else {}
    order = research.get("research_order") if isinstance(research.get("research_order"), list) else []
    failures = intelligence.get("failures") if isinstance(intelligence.get("failures"), dict) else {}
    flags = [f"SOURCE_FAILURE:{k}" for k, v in failures.items() if v]
    if not regime.get("regime"): flags.append("REGIME_NOT_AVAILABLE")
    if not validation.get("trial_registry_complete", False): flags.append("TRIAL_REGISTRY_NOT_VERIFIED")
    if not validation.get("walk_forward_purge_complete", False): flags.append("WALK_FORWARD_PURGE_NOT_VERIFIED")
    observations = [f"Computed regime: {regime['regime']}." if regime.get("regime") else "No computed regime label was supplied; none is inferred."]
    metrics = []
    for strategy in order:
        row = oos.get(strategy)
        if not isinstance(row, dict) or row.get("status") != "OK": continue
        sharpe, ret, drawdown = (_number(row.get(k)) for k in ("sharpe", "oos_total_return", "max_drawdown"))
        if sharpe is not None and ret is not None: metrics.append((str(strategy), sharpe, ret, drawdown))
    if metrics:
        metrics.sort(key=lambda row: (row[1], row[2]), reverse=True)
        n, sharpe, ret, drawdown = metrics[0]
        observations.append(f"Highest available OOS measurement by Sharpe is {n} (Sharpe {sharpe:.3f}, return {ret:.4f}, drawdown {'unavailable' if drawdown is None else f'{drawdown:.4f}'}); measurement alone cannot promote a strategy.")
    else:
        observations.append("No valid OOS measurement was available; no winner is selected.")
        flags.append("NO_VALID_OOS_MEASUREMENT")
    news = intelligence.get("news_summary") if isinstance(intelligence.get("news_summary"), dict) else {}
    article_count = news.get("article_count")
    if isinstance(article_count, int) and not isinstance(article_count, bool) and article_count >= 0:
        observations.append(f"News aggregation reported {article_count} articles; count is not a trade signal.")
    candidates = research.get("paper_candidates")
    if not isinstance(candidates, list): candidates = []
    confidence = 0.55 if metrics else 0.2
    if flags: confidence = max(0.1, confidence - min(0.35, 0.05 * len(flags)))
    return {"engine": "atlas-native-research-v1", "regime_assessment": str(regime.get("regime") or "UNKNOWN"),
        "key_observations": observations, "data_quality_flags": sorted(set(flags)),
        "strategy_watchlist": [str(x.get("strategy")) for x in candidates if isinstance(x, dict) and x.get("strategy")],
        "confidence": round(confidence, 3), "research_only_summary": " ".join(observations),
        "external_intelligence": {"news_summary": news, "source_failures": failures},
        "execution_authority": False, "promotion_authority": False, "network_calls": 0}


def evaluate_native_trade_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed local veto for malformed, unsafe or inconsistent decision evidence."""
    packet = packet if isinstance(packet, dict) else {}
    required = ("symbol", "asset", "exchange", "timeframe", "market_timestamp", "trade_plan", "deterministic_rules",
                "price_context", "data_quality", "adaptive_model", "ml_signal")
    reasons = [f"missing_{key}" for key in required if key not in packet or packet.get(key) is None]
    if reasons:
        return {"safe": False, "confidence": 0.0, "reasons": reasons, "data_quality_flags": ["INCOMPLETE_PACKET"],
                "engine": "atlas-native-safety-v1", "execution_authority": False}
    if not str(packet.get("symbol") or "").strip() or packet.get("asset") not in _ALLOWED_ASSETS or not str(packet.get("exchange") or "").strip():
        reasons.append("invalid_market_identity")
    if packet.get("timeframe") not in _ALLOWED_TIMEFRAMES or not packet.get("market_timestamp"): reasons.append("invalid_market_time_context")
    plan = packet.get("trade_plan")
    if not isinstance(plan, dict): plan = {}; reasons.append("trade_plan_not_object")
    quality = packet.get("data_quality")
    if not isinstance(quality, dict) or quality.get("safe") is not True: reasons.append("market_data_quality_not_safe")
    elif quality.get("reasons"): reasons.append("market_data_quality_has_reasons")
    if not isinstance(packet.get("deterministic_rules"), dict): reasons.append("deterministic_rules_missing")
    price = packet.get("price_context")
    if not isinstance(price, dict): price = {}; reasons.append("price_context_missing")
    close, spread = _number(price.get("latest_close")), _number(price.get("spread_bps"))
    bars = price.get("bars")
    if close is None or close <= 0: reasons.append("invalid_latest_close")
    if isinstance(bars, bool) or not isinstance(bars, int) or bars < 1: reasons.append("invalid_bar_count")
    if spread is None or spread < 0: reasons.append("invalid_spread")
    adaptive = packet.get("adaptive_model")
    if not isinstance(adaptive, dict) or not adaptive: reasons.append("adaptive_model_evidence_missing")
    ml = packet.get("ml_signal")
    if not isinstance(ml, dict): ml = {}; reasons.append("ml_signal_missing")
    signal = _number(ml.get("signal"))
    adaptive_disabled = (
        isinstance(adaptive, dict)
        and str(adaptive.get("status") or "").upper() == "DISABLED"
        and isinstance(ml, dict)
        and str(ml.get("status") or "").upper() == "DISABLED"
        and ml.get("signal") is None
    )
    data_quality_flags = ["ADAPTIVE_AI_DISABLED"] if adaptive_disabled else []
    if not adaptive_disabled and (signal is None or signal == 0):
        reasons.append("ml_signal_is_not_directional")
    side = str(plan.get("side") or "").strip().lower()
    entry, stop, target = (_number(plan.get(key)) for key in ("entry_price", "stop_loss_price", "take_profit_price"))
    if side not in {"buy", "sell"}: reasons.append("invalid_trade_side")
    if any(x is None or x <= 0 for x in (entry, stop, target)): reasons.append("invalid_protective_price")
    elif side == "buy":
        if not stop < entry < target: reasons.append("long_stop_entry_target_order_invalid")
        if not adaptive_disabled and signal is not None and signal <= 0: reasons.append("ml_signal_direction_mismatch")
    elif side == "sell":
        if not target < entry < stop: reasons.append("short_target_entry_stop_order_invalid")
        if not adaptive_disabled and signal is not None and signal >= 0: reasons.append("ml_signal_direction_mismatch")
    if entry is not None and stop is not None and target is not None and abs(entry - stop) > 0:
        if abs(target - entry) / abs(entry - stop) < 1.5: reasons.append("reward_risk_below_native_minimum")
    else: reasons.append("risk_distance_invalid")
    if reasons:
        return {"safe": False, "confidence": 0.0, "reasons": sorted(set(reasons)), "data_quality_flags": data_quality_flags,
                "engine": "atlas-native-safety-v1", "execution_authority": False}
    return {"safe": True, "confidence": 1.0, "reasons": ["all_native_safety_checks_passed"], "data_quality_flags": data_quality_flags,
            "engine": "atlas-native-safety-v1", "execution_authority": False}


async def native_trade_safety_review(packet: dict[str, Any]) -> dict[str, Any]:
    result = evaluate_native_trade_packet(packet)
    return {"mode": "ATLAS_NATIVE_AI_SAFETY_VETO", "safe": result["safe"], "configured": True,
            "providers": {"atlas_native": result}, "errors": {}, "execution_authority": False,
            **({"reasons": result["reasons"]} if not result["safe"] else {})}


def native_ai_health_check() -> dict[str, Any]:
    draft = compose_strategy_spec(prompt="BTC trend strategy on 1h, risk 0.5%", name="Native check")
    keys = {"strategy_family", "entry_conditions", "exit_conditions", "requires_backtest", "requires_walk_forward", "execution_mode"}
    incomplete = evaluate_native_trade_packet({"symbol": "BTC/USDT"})["safe"] is False
    review = native_research_review({}, {})
    ok = keys.issubset(draft) and draft["strategy_family"] in _ALLOWED_FAMILIES and draft["execution_mode"] == "PAPER_SHADOW_ONLY" and incomplete and review["execution_authority"] is False and review["network_calls"] == 0
    return {"status": "PASS" if ok else "FAIL", "engine": "atlas-native-ai-v1", "network_calls": 0,
        "external_model_credentials_required": False, "checks": {"strategy_composer": keys.issubset(draft),
        "incomplete_trade_fails_closed": incomplete, "research_is_advisory": review["execution_authority"] is False}}


async def dual_ai_trade_safety_review(trade_packet: dict[str, Any]) -> dict[str, Any]:
    """Legacy call signature; executes the local veto without provider calls."""
    return await native_trade_safety_review(trade_packet)


async def dual_ai_research_review(research: dict[str, Any], intelligence: dict[str, Any]) -> dict[str, Any]:
    review = native_research_review(research, intelligence)
    return {"mode": "ATLAS_NATIVE_RESEARCH_REVIEW", "providers": {"atlas_native": review}, "errors": {},
        "execution_authority": False, "network_calls": 0}
