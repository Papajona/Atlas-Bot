"""Atlas Native AI: constrained local strategy composition and deterministic review.

No hosted model, model API key, external inference endpoint or network call is used.
This is not a general-purpose generative language model. Statistical prediction continues
to use Atlas's existing locally executed adaptive ML pipeline.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple


class NativeAIError(RuntimeError):
    """Raised when the local Native AI engine cannot produce a valid draft."""


class NativeAIInputError(NativeAIError):
    """Raised when a caller supplies invalid strategy input rather than an engine failure."""


AIProviderError = NativeAIError  # compatibility alias; no provider is called
_ALLOWED_TIMEFRAMES = ("5m", "15m", "30m", "1h", "4h", "1d")
_ALLOWED_ASSETS = ("crypto", "forex", "commodity")
_ALLOWED_FAMILIES = ("TREND", "MOMENTUM", "BREAKOUT", "MEAN_REVERSION", "ENSEMBLE")
_TIMEFRAME_SECONDS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
_SPREAD_CEILING_BPS = 300.0
_MAX_ENTRY_DEVIATION = 0.10
_MAX_STOP_DISTANCE = 0.50
_FUTURE_SKEW_SECONDS = 300
_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_:.\-]{0,79}$")
_NEGATED_DIRECTION = re.compile(
    r"\b(?:do\s+not|don'?t|never|no|not|avoid|without|except|exclude|excluding)\s+"
    r"(?:(?:to|going|go|be|take|open|any)\s+)*"
    r"(?:buy\w*|long\w*|short\w*|sell\w*|bearish|bullish)\b"
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _parse_market_time(value: Any) -> datetime | None:
    """Parse ISO-8601 text, datetime or epoch seconds/milliseconds into aware UTC."""
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, bool):
            return None
        elif isinstance(value, (int, float)):
            number = float(value)
            if not math.isfinite(number) or number <= 0:
                return None
            parsed = datetime.fromtimestamp(number / 1000.0 if number > 1e11 else number, tz=timezone.utc)
        elif isinstance(value, str) and value.strip():
            text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
            text = re.sub(r"(\.\d{6})\d+", r"\1", text)
            parsed = datetime.fromisoformat(text)
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _strip_negated_direction(text: str) -> str:
    """Remove negated direction phrases before detecting a requested trade side."""
    return _NEGATED_DIRECTION.sub(" ", text)


def _classify_asset(text: str, requested: str | None) -> str:
    if requested in _ALLOWED_ASSETS:
        return requested
    value = text.lower()
    # Match complete instrument names/tickers: "golden cross" is not Gold, and "forexample" is not Forex.
    if re.search(r"\b(?:forex|currency|currencies|eur\s*[/_-]?\s*usd|gbp\s*[/_-]?\s*usd|usd\s*[/_-]?\s*jpy)\b", value):
        return "forex"
    if re.search(r"\b(?:commodity|commodities|gold|xau(?:\s*[/_-]?\s*usd)?|wti|brent|crude\s+oil|natural\s+gas|silver|xag(?:\s*[/_-]?\s*usd)?)\b", value):
        return "commodity"
    return "crypto"


_SYMBOL_ASSET = {
    "EUR_USD": "forex", "GBP_USD": "forex", "USD_JPY": "forex",
    "XAU_USD": "commodity", "XAG_USD": "commodity", "WTI": "commodity",
    "WTICO_USD": "commodity", "BCO_USD": "commodity", "NATGAS_USD": "commodity",
    "BTC/USDT": "crypto", "ETH/USDT": "crypto",
}


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


_FAMILY_HINT = re.compile(
    r"\b(?:trend|trending|moving\s+average|ema|sma|golden\s+cross|death\s+cross|mean[-\s]?reversion|"
    r"revert|range[-\s]?bound|oversold|overbought|breakout|break\s+out|prior\s+high|range\s+high|range\s+low|"
    r"momentum|roc|rate\s+of\s+change|acceleration|ensemble|combine|multiple\s+strategies)\b")


def _family_was_inferred(text: str) -> bool:
    """True when the prompt named a strategy type; False when TREND is only the fallback."""
    return _FAMILY_HINT.search(text.lower()) is not None


_PROFILE_NEGATION_PREFIX = re.compile(
    r"\b(?:non[-\s]+|do\s+not|does\s+not|did\s+not|don't|dont|doesn't|doesnt|never|"
    r"is\s+not|are\s+not|was\s+not|were\s+not|isn't|isnt|aren't|arent|"
    r"not|no|avoid|without|exclude|excluding|reject(?:ed)?)\b"
    r"(?:\s+(?:please|use|using|choose|select|apply|adopt|enable|switch|to|the|a|an|"
    r"this|that|very|too|profile|mode|setting|strategy|approach)){0,6}\s*$"
)
_CONSERVATIVE_RE = re.compile(
    r"\b(?:conservative(?:ly)?|cautious(?:ly)?|defensive|capital\s+preservation|preserve\s+capital|"
    r"low[-\s]?risk|risk[-\s]?averse|safe(?:ly|r|st)?)\b")
_ADAPTIVE_RE = re.compile(
    r"\b(?:adaptive|adaptively|regime[-\s]?(?:aware|adaptive|switching|based|filter(?:ed)?)|self[-\s]?adjust(?:ing)?|"
    r"volatility[-\s]?(?:adjusted|scaled|aware)|dynamic(?:ally)?\s+(?:size|sizing|adjust\w*))\b")
_AGGRESSIVE_RE = re.compile(r"\b(?:aggressive(?:ly)?|high[-\s]?risk|max(?:imum)?\s+leverage|all[-\s]?in|yolo)\b")


def _profile_flag(pattern: re.Pattern[str], lower: str) -> bool:
    """Match a profile keyword unless the surrounding prompt explicitly rejects it."""
    for match in pattern.finditer(lower):
        before = lower[max(0, match.start() - 80):match.start()]
        # A negation applies only within its local clause; a later corrective clause such
        # as "but use adaptive" must not inherit an earlier "do not use conservative".
        before = re.split(r"[.!?;,:]|\b(?:but|however|instead|rather)\b", before)[-1]
        # A non- prefix ends in a hyphen/space (both non-word characters),
        # so the generic trailing word-boundary assertion cannot match the slice.
        if (
            _PROFILE_NEGATION_PREFIX.search(before)
            or re.search(r"\\bnon[-\\s]+$", before)
        ):
            continue
        return True
    return False


def _detect_profiles(lower: str) -> tuple[bool, bool, bool]:
    """Return (conservative, adaptive, aggressive) request flags from the prompt."""
    conservative = _profile_flag(_CONSERVATIVE_RE, lower)
    aggressive = _profile_flag(_AGGRESSIVE_RE, lower)
    if conservative and aggressive:  # contradictory: the safer reading wins and the caller is told
        aggressive = False
    return conservative, _profile_flag(_ADAPTIVE_RE, lower), aggressive


# Profile conditions are free-form StrategyCondition types, so no schema change is needed.
_CONSERVATIVE_ENTRY = [
    {"type": "HIGHER_TIMEFRAME_AGREEMENT", "rule": "Require the next-higher timeframe trend/regime to agree with the proposed side on completed bars; otherwise stand aside."},
    {"type": "CONSERVATIVE_VOLATILITY_CAP", "rule": "Skip entries when ATR percentile or spread is above the configured cap, and stand aside around scheduled high-impact news."}]
_ADAPTIVE_ENTRY = [
    {"type": "REGIME_ADAPTATION", "rule": "Classify trend versus range and low versus high volatility on completed bars; enable trend/breakout/momentum rules only in a trending regime and mean-reversion rules only in a ranging regime."},
    {"type": "VOLATILITY_SCALED_RISK", "rule": "Scale position size down as realized volatility rises so cash risk per trade stays at or below the configured fraction."}]
_CONSERVATIVE_EXIT = {"type": "TIME_STOP", "rule": "Close a trade that has not reached its target or stop within a configured number of bars; never widen a stop after entry."}


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




_SUPPORTED_BY_MINUTES = {5: "5m", 15: "15m", 30: "30m", 60: "1h", 240: "4h", 1440: "1d"}
_COMPACT_SUPPORTED = frozenset(_ALLOWED_TIMEFRAMES)
_COMPACT_UNSUPPORTED = frozenset({"1m", "2m", "3m", "10m", "20m", "45m", "2h", "3h", "6h", "8h", "12h", "3d", "1w", "2w"})
_UNIT_MINUTES = {
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "d": 1440, "day": 1440, "days": 1440,
    "w": 10080, "week": 10080, "weeks": 10080,
}
_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "fifteen": 15, "thirty": 30}
_TF_COMPACT_RE = re.compile(r"(?<![a-z0-9$.])(\d{1,3}(?:m|h|d|w))(?![a-z0-9])")
_TF_LONG_RE = re.compile(r"(?<![a-z0-9$.])(\d{1,3}|one|two|three|four|five|fifteen|thirty)[\s-]?(minutes?|mins?|hours?|hrs?|days?|weeks?)(?![a-z0-9])")
_TF_NAMED_RE = re.compile(r"(?<![a-z0-9])(daily|hourly|weekly)(?![a-z0-9])(?!\s+(?:loss|limit|risk|drawdown|target|profit|max|cap|stop))")
_TF_CONTEXT_AFTER = re.compile(r"\s*(?:time\s*frame|timeframe|chart|bars?|candles?|candlesticks?|interval)s?\b")
_TF_CONTEXT_BEFORE = re.compile(r"(?:\bon(?:\s+the|\s+a)?|\busing(?:\s+the|\s+a)?|\bin\s+the|\bover\s+the)\s*$")


class _TFMention(NamedTuple):
    start: int
    end: int
    tf: str | None
    raw: str


def _timeframe_mentions(lower: str) -> list[_TFMention]:
    """Find supported and known-unsupported timeframe requests without mistaking lookbacks for charts."""
    found: list[_TFMention] = []
    for match in _TF_COMPACT_RE.finditer(lower):
        token = match.group(1)
        if token in _COMPACT_SUPPORTED:
            found.append(_TFMention(match.start(1), match.end(1), token, token))
        elif token in _COMPACT_UNSUPPORTED:
            found.append(_TFMention(match.start(1), match.end(1), None, token))
    for match in _TF_LONG_RE.finditer(lower):
        if not (_TF_CONTEXT_AFTER.match(lower, match.end()) or _TF_CONTEXT_BEFORE.search(lower[max(0, match.start() - 14):match.start()])):
            continue
        number = _WORD_NUMBERS.get(match.group(1)) or int(match.group(1))
        minutes = number * _UNIT_MINUTES[match.group(2)]
        found.append(_TFMention(match.start(), match.end(), _SUPPORTED_BY_MINUTES.get(minutes), match.group(0)))
    for match in _TF_NAMED_RE.finditer(lower):
        word = match.group(1)
        found.append(_TFMention(match.start(), match.end(), {"daily": "1d", "hourly": "1h"}.get(word), word))
    found.sort(key=lambda mention: mention.start)
    # Prefer the longest overlapping mention (e.g. "daily timeframe" is one phrase).
    deduplicated: list[_TFMention] = []
    for mention in found:
        if deduplicated and mention.start < deduplicated[-1].end:
            if mention.end > deduplicated[-1].end:
                deduplicated[-1] = mention
            continue
        deduplicated.append(mention)
    return deduplicated


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


_ATR_RE = re.compile(r"(?<![\w.])(\d{1,6}(?:\.\d{1,6})?)\s{0,3}-?\s{0,3}atr\b(?!\s{0,3}(?:period|length|lookback|bars?|candles?))")
_STOP_WORDS = re.compile(r"\b(?:stop|sl|stop-?loss|loss|risk)\b")
_TARGET_WORDS = re.compile(r"\b(?:take[-\s]?profit|tp|target|profit)\b")
_NUM = r"(\d{1,6}(?:\.\d{1,6})?)"
_RR_AFTER_RE = re.compile(
    r"(?:(?P<rw>reward)\s{0,3}(?:[:/]|-|to)?\s{0,3}risk|(?P<rk>risk)\s{0,3}(?:[:/]|-|to)?\s{0,3}reward|(?P<rr>\brr\b)|\br\s{0,3}[:/]\s{0,3}r\b)"
    rf"\s{{0,3}}(?:ratio|target)?\s{{0,3}}(?:of|=|:|is|at)?\s{{0,3}}{_NUM}(?:\s{{0,3}}[:/]\s{{0,3}}{_NUM})?")
_RR_BEFORE_RE = re.compile(
    rf"(?<![\w.]){_NUM}\s{{0,3}}[:/]\s{{0,3}}{_NUM}\s{{0,3}}(?:(?P<rw>reward\s{{0,3}}[:/]?\s{{0,3}}risk)|risk\s{{0,3}}[:/]?\s{{0,3}}reward|\brr\b|\br\s{{0,3}}[:/]\s{{0,3}}r\b)")
_RR_R_SUFFIX_RE = re.compile(r"(?:take.?profit|reward.?risk|risk.?reward|target)\s{0,3}(?:of|=|:)?\s{0,3}(\d{1,6}(?:\.\d{1,6})?)\s{0,3}r\b")
_RR_R_LEAD_RE = re.compile(r"(?<![\w.])(\d{1,6}(?:\.\d{1,6})?)\s{0,3}r\s{1,3}(?:take[-\s]?profit|target|tp)\b")


def _atr_levels(lower: str) -> tuple[list[float], list[float], list[float]]:
    stops: list[float] = []
    targets: list[float] = []
    plain: list[float] = []
    for match in _ATR_RE.finditer(lower):
        value = float(match.group(1))
        before = re.split(r"[.;,]| and | then | with ", lower[max(0, match.start() - 28):match.start()])[-1]
        after = re.split(r"[.;,]| and | then | with ", lower[match.end():match.end() + 14])[0]
        if _STOP_WORDS.search(before) or re.match(r"\s*(?:stop|sl)\b", after):
            stops.append(value)
        elif _TARGET_WORDS.search(before) or re.match(r"\s*(?:take[-\s]?profit|tp|target|profit)\b", after):
            targets.append(value)
        else:
            plain.append(value)
    return stops, targets, plain


def _reward_risk(lower: str) -> float | None:
    match = _RR_AFTER_RE.search(lower)
    if match:
        first, second = float(match.group(4)), match.group(5)
        if second is None:
            return first or None
        second_value = float(second)
        if not first or not second_value:
            return None
        return first / second_value if match.group("rw") else second_value / first
    match = _RR_BEFORE_RE.search(lower)
    if match:
        first, second = float(match.group(1)), float(match.group(2))
        if not first or not second:
            return None
        return first / second if match.group("rw") else second / first
    match = _RR_R_SUFFIX_RE.search(lower) or _RR_R_LEAD_RE.search(lower)
    return float(match.group(1)) if match else None


def _parse_stop_and_target(lower: str, default_stop_atr: float = 1.5, default_reward: float = 2.0) -> tuple[float, float, list[str]]:
    notes: list[str] = []
    stops, targets, plain = _atr_levels(lower)
    stop = stops[0] if stops else (plain[0] if plain else None)
    stop_atr = _clamp(stop, 0.1, 10.0) if stop is not None else default_stop_atr
    reward = _reward_risk(lower)
    if reward is None and targets:
        reward = targets[0] / stop_atr
        if stop is None:
            notes.append(f"Take-profit of {targets[0]:g} ATR was converted to {reward:.2f}R using the default {stop_atr:g} ATR stop.")
    if reward is None:
        if re.search(r"(?:reward|\brr\b)[^.;]{0,20}\d", lower):
            notes.append("A reward:risk target was mentioned but not understood; the default 2.0R was used. Write it as '3R' or 'reward risk 3'.")
        reward = default_reward
    clamped = _clamp(reward, 0.1, 10.0)
    if clamped != reward:
        notes.append(f"Requested reward:risk {reward:g}R was limited to {clamped:g}R.")
    return stop_atr, clamped, notes


def _condition_timeframe(prompt: str, condition_type: str, default_timeframe: str) -> str:
    """Bind supported rules to the nearest explicitly requested timeframe when clear."""
    prompt_lower = prompt.lower()
    timeframe_matches = [mention for mention in _timeframe_mentions(prompt_lower) if mention.tf][:20]
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
            max(0, max(start - timeframe_match.end, timeframe_match.start - end))
            for start, end in keyword_spans
        )
        ranked.append((distance, timeframe_match.tf))
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
        selected_symbol = symbol.strip()
        if not _SYMBOL_PATTERN.fullmatch(selected_symbol):
            raise NativeAIInputError("Symbol may contain only letters, digits and / _ : . - (max 80 characters).")
        inferred_symbol_asset = _SYMBOL_ASSET.get(selected_symbol)
        if inferred_symbol_asset and asset not in _ALLOWED_ASSETS:
            asset_kind = inferred_symbol_asset
    else:
        upper = text.upper()
        match = next(((pattern, label) for pattern, label in (
            (r"\bEUR\s*[/_-]?\s*USD\b", "EUR_USD"),
            (r"\bGBP\s*[/_-]?\s*USD\b", "GBP_USD"),
            (r"\bXAU(?:\s*[/_-]?\s*USD)?\b|\bGOLD\b", "XAU_USD"),
            (r"\bXAG(?:\s*[/_-]?\s*USD)?\b|\bSILVER\b", "XAG_USD"),
            (r"\bBTC(?:\s*[/_-]?\s*USDT?)?\b", "BTC/USDT"),
            (r"\bETH(?:\s*[/_-]?\s*USDT?)?\b", "ETH/USDT"),
            (r"\bWTI\b|\bCRUDE\s*OIL\b", "WTI"),
        ) if re.search(pattern, upper)), None)
        selected_symbol = match[1] if match else {"crypto": "BTC/USDT", "forex": "EUR_USD", "commodity": "XAU_USD"}[asset_kind]
        if match and asset not in _ALLOWED_ASSETS:
            asset_kind = _SYMBOL_ASSET.get(match[1], asset_kind)
    explicit_timeframe = timeframe if timeframe in _ALLOWED_TIMEFRAMES else None
    mentions = _timeframe_mentions(text.lower())
    prompt_timeframes = list(dict.fromkeys(mention.tf for mention in mentions if mention.tf))[:5]
    unsupported_timeframes = list(dict.fromkeys(mention.raw for mention in mentions if not mention.tf))
    # A selected UI/API timeframe is authoritative. Only infer multiple condition-level
    # timeframes from the prompt when the caller leaves the timeframe unset.
    requested_timeframes = (
        [explicit_timeframe]
        if explicit_timeframe
        else prompt_timeframes or ["1h"]
    )
    tf = requested_timeframes[0]
    family = _classify_family(text)
    lower = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    # Curly apostrophes are common on mobile keyboards and must share ASCII negation semantics.
    direction_source = lower.replace("\u2019", "'").replace("\u2018", "'")
    direction_text = _strip_negated_direction(direction_source)
    negated_direction = direction_text != direction_source
    asks_both = any(x in direction_text for x in ("both directions", "long and short", "long/short", "both sides"))
    # "short EMA" and "long EMA" describe indicator periods, while "sell when RSI"
    # describes an exit. Only explicit direction phrases or a known instrument imply side.
    known_symbol = (
        r"(?:btc(?:\s*[/_-]?\s*usdt?)?|bitcoin|eth(?:\s*[/_-]?\s*usdt?)?|ethereum|"
        r"eur\s*[/_-]?\s*usd|gbp\s*[/_-]?\s*usd|xau(?:\s*[/_-]?\s*usd)?|gold|"
        r"xag(?:\s*[/_-]?\s*usd)?|silver|wti|crude\s+oil)"
    )
    asks_short = bool(
        re.search(r"\b(?:bearish|downtrend|shorting|go\s+short|short\s+only|short[-\s]sell)\b", direction_text)
        or re.search(rf"\bshort\s+(?:the\s+)?{known_symbol}\b", direction_text)
        or re.search(r"\bsell\s+only\b", direction_text)
        or re.search(rf"\bsell\s+(?:the\s+)?{known_symbol}\b", direction_text)
    )
    asks_long = bool(
        re.search(r"\b(?:buy|bullish|uptrend)\b", direction_text)
        or re.search(r"\blong\b(?!\s+(?:ema|sma|ma|moving\s+average|window|period|lookback|average)\b)", direction_text)
    )
    direction_ambiguous = not (asks_both or asks_short or asks_long)
    direction = (
        "BOTH" if asks_both or (asks_short and asks_long)
        else "SHORT" if asks_short
        else "LONG" if asks_long
        else "AUTO" if negated_direction
        else "LONG"
    )
    cap = _number(max_risk_per_trade)
    cap = min(cap if cap is not None and cap > 0 else 0.005, 0.05)
    risk = min(cap, 0.005)
    warnings = [
        "Generated locally by Atlas Native AI from supported templates; not a general-purpose language-model response.",
        "This draft is not backtest evidence; cost-aware out-of-sample and walk-forward validation remain mandatory before paper promotion."]
    supported_list = ", ".join(_ALLOWED_TIMEFRAMES)
    if unsupported_timeframes:
        warnings.append(
            f"Unsupported timeframe(s) in the prompt were ignored: {', '.join(unsupported_timeframes)}. "
            f"Supported: {supported_list}. The draft uses {', '.join(requested_timeframes)}; confirm before use.")
    if timeframe and not explicit_timeframe:
        warnings.append(f"Selected timeframe {str(timeframe)[:12]!r} is not supported and was ignored. Supported: {supported_list}.")
    if direction_ambiguous:
        warnings.append(
            "No affirmative trading direction was supplied; AUTO is used when the prompt only excludes a direction."
            if negated_direction
            else "No trading direction was explicit in the prompt; LONG is only a draft default and must be confirmed before use."
        )
    if negated_direction:
        warnings.append("Negated direction wording was detected and excluded from side detection; confirm the permitted direction before use.")
    if explicit_timeframe and len(prompt_timeframes) > 1 and any(
        prompt_timeframe != explicit_timeframe for prompt_timeframe in prompt_timeframes
    ):
        warnings.append(
            f"Selected timeframe {explicit_timeframe} overrides the draft's multi-timeframe setup inferred from the prompt."
        )
    risk_match = re.search(r"\brisk(?:\s+per\s+trade)?\s{0,6}(?:of|=|:)?\s{0,6}(\d{1,6}(?:\.\d{1,6})?)\s{0,6}(%|percent|bps|bp)?(?![a-z])", lower)
    if risk_match:
        amount = float(risk_match.group(1))
        unit = (risk_match.group(2) or "").lower()
        requested = amount / (10000.0 if unit in {"bp", "bps"} else 100.0 if unit in {"%", "percent"} or amount > 0.05 else 1.0)
        if math.isfinite(requested) and requested > 0:
            risk = min(cap, requested)
            if requested > cap:
                warnings.append(f"Requested risk capped at platform maximum {cap:.4f}.")
    conservative, adaptive, aggressive = _detect_profiles(lower)
    profile_notes: list[str] = []
    if conservative and not risk_match:
        risk = min(risk, 0.0025)  # half the platform default; an explicit risk in the prompt still wins
    stop_atr, target_rr, stop_notes = _parse_stop_and_target(
        lower, default_stop_atr=2.0 if conservative else 1.5)
    warnings.extend(stop_notes)
    if conservative:
        profile_notes.append("Conservative profile: default risk 0.25%, default 2 ATR stop (explicit values in the prompt still apply), higher-timeframe agreement, volatility cap and a time stop were added.")
    if adaptive:
        profile_notes.append("Adaptive profile: regime classification and volatility-scaled sizing rules were added; regime thresholds are not tuned here and must be validated out of sample.")
    if aggressive:
        profile_notes.append("Aggressive wording was ignored: risk is never raised above the platform cap and the default balanced template was kept.")
    if not _family_was_inferred(text):
        profile_notes.append("No strategy type was recognised in the prompt; the TREND template is only a draft default, so confirm or name a strategy type.")
    warnings.extend(profile_notes)
    entries, exits = _template_rules(family)
    if conservative:
        entries = entries + _CONSERVATIVE_ENTRY
        exits = exits + [_CONSERVATIVE_EXIT]
    if adaptive:
        entries = entries + _ADAPTIVE_ENTRY
    if explicit_timeframe:
        entry_conditions = [dict(x, timeframe=tf) for x in entries]
        exit_conditions = [dict(x, timeframe=tf) for x in exits]
    else:
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
        "validation_warnings": warnings[:12], "requires_backtest": True, "requires_walk_forward": True,
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
    if packet.get("timeframe") not in _ALLOWED_TIMEFRAMES or not packet.get("market_timestamp"):
        reasons.append("invalid_market_time_context")
    else:
        market_time = _parse_market_time(packet.get("market_timestamp"))
        if market_time is None:
            reasons.append("invalid_market_timestamp")
        elif market_time > datetime.now(timezone.utc) + timedelta(seconds=_FUTURE_SKEW_SECONDS):
            reasons.append("market_timestamp_in_future")
    plan = packet.get("trade_plan")
    if not isinstance(plan, dict): plan = {}; reasons.append("trade_plan_not_object")
    quality = packet.get("data_quality")
    if not isinstance(quality, dict) or quality.get("safe") is not True: reasons.append("market_data_quality_not_safe")
    elif quality.get("reasons"): reasons.append("market_data_quality_has_reasons")
    rules = packet.get("deterministic_rules")
    if not isinstance(rules, dict) or not rules:
        reasons.append("deterministic_rules_missing")
    price = packet.get("price_context")
    if not isinstance(price, dict): price = {}; reasons.append("price_context_missing")
    close, spread = _number(price.get("latest_close")), _number(price.get("spread_bps"))
    bars = price.get("bars")
    if close is None or close <= 0: reasons.append("invalid_latest_close")
    if isinstance(bars, bool) or not isinstance(bars, int) or bars < 1: reasons.append("invalid_bar_count")
    if spread is None or spread < 0:
        reasons.append("invalid_spread")
    else:
        spread_limit = _SPREAD_CEILING_BPS
        configured_limit = _number(price.get("max_spread_bps"))
        if configured_limit is not None and 0 < configured_limit < spread_limit:
            spread_limit = configured_limit
        if spread > spread_limit:
            reasons.append("spread_exceeds_limit")
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
        if abs(target - entry) / abs(entry - stop) < 1.5:
            reasons.append("reward_risk_below_native_minimum")
        stop_fraction = abs(entry - stop) / entry if entry > 0 else None
        if stop_fraction is not None:
            if stop_fraction > _MAX_STOP_DISTANCE:
                reasons.append("stop_distance_extreme")
            if spread is not None and spread >= 0 and stop_fraction * 10000.0 <= spread:
                reasons.append("stop_distance_not_above_spread")
    else:
        reasons.append("risk_distance_invalid")
    if entry is not None and entry > 0 and close is not None and close > 0:
        if abs(entry - close) / close > _MAX_ENTRY_DEVIATION:
            reasons.append("entry_price_far_from_latest_close")

    groq_required = packet.get("groq_trade_review_required", False)
    if type(groq_required) is not bool:
        reasons.append("groq_trade_review_requirement_invalid")
    elif groq_required:
        envelope = packet.get("groq_trade_review")
        if (
            not isinstance(envelope, dict)
            or envelope.get("status") != "OK"
            or envelope.get("provider") != "groq"
            or envelope.get("execution_authority") is not False
        ):
            reasons.append("groq_trade_review_unavailable")
        elif envelope.get("input_truncated") is not False:
            reasons.append("groq_trade_review_input_incomplete")
        else:
            review = envelope.get("review")
            if not isinstance(review, dict):
                reasons.append("groq_trade_review_malformed")
            else:
                alignment = str(review.get("setup_alignment") or "").upper()
                risk_level = str(review.get("risk_level") or "").upper()
                groq_confidence = _number(review.get("confidence"))
                valid_alignment = alignment in {"ALIGNED", "CONTRADICTS", "NEUTRAL", "INSUFFICIENT_EVIDENCE"}
                valid_risk = risk_level in {"LOW", "MODERATE", "HIGH", "CRITICAL", "UNKNOWN"}
                if not valid_alignment or not valid_risk or groq_confidence is None or not 0.0 <= groq_confidence <= 1.0:
                    reasons.append("groq_trade_review_malformed")
                else:
                    data_quality_flags.append("GROQ_TRADE_REVIEW_INCLUDED")
                    if alignment == "INSUFFICIENT_EVIDENCE" or risk_level == "UNKNOWN" or groq_confidence < 0.50:
                        reasons.append("groq_trade_evidence_insufficient")
                    if alignment == "CONTRADICTS":
                        reasons.append("groq_setup_contradiction_veto")
                    # HIGH and CRITICAL are veto levels regardless of Groq's
                    # self-reported confidence, matching the reviewed prompt contract.
                    if risk_level == "CRITICAL":
                        reasons.append("groq_critical_risk_veto")
                    elif risk_level == "HIGH":
                        reasons.append("groq_high_risk_veto")
    if reasons:
        return {"safe": False, "confidence": 0.0, "reasons": sorted(set(reasons)), "data_quality_flags": data_quality_flags,
                "engine": "atlas-native-safety-v1", "execution_authority": False}
    return {"safe": True, "confidence": 1.0, "reasons": ["all_native_safety_checks_passed"], "data_quality_flags": data_quality_flags,
            "engine": "atlas-native-safety-v1", "execution_authority": False}


async def native_trade_safety_review(packet: dict[str, Any]) -> dict[str, Any]:
    result = evaluate_native_trade_packet(packet)
    providers = {"atlas_native": result}
    groq = packet.get("groq_trade_review") if isinstance(packet, dict) else None
    if isinstance(groq, dict):
        # Preserve bounded review evidence and safe failure diagnostics; provider
        # output remains advisory and can never authorize execution.
        groq_audit = {
            key: groq[key]
            for key in ("status", "provider", "model", "input_truncated", "execution_authority", "review")
            if key in groq
        }
        for key in ("error", "cause"):
            value = groq.get(key)
            if (
                isinstance(value, str)
                and value
                and len(value) <= 80
                and value.isascii()
                and all(char.isalnum() or char in "._" for char in value)
            ):
                groq_audit[key] = value
        http_status = groq.get("http_status")
        if (
            isinstance(http_status, int)
            and not isinstance(http_status, bool)
            and 100 <= http_status <= 599
        ):
            groq_audit["http_status"] = http_status
        reason = groq.get("reason")
        if (
            isinstance(reason, str)
            and reason
            and len(reason) <= 80
            and reason.isascii()
            and all(char.isalnum() or char in "._-" for char in reason)
        ):
            groq_audit["reason"] = reason
        providers["groq"] = groq_audit
    return {"mode": "ATLAS_NATIVE_AI_SAFETY_VETO", "safe": result["safe"], "configured": True,
            "providers": providers, "errors": {}, "execution_authority": False,
            **({"reasons": result["reasons"]} if not result["safe"] else {})}


def native_ai_health_check() -> dict[str, Any]:
    draft = compose_strategy_spec(prompt="BTC trend strategy on 1h, risk 0.5%", name="Native check")
    keys = {"strategy_family", "entry_conditions", "exit_conditions", "requires_backtest", "requires_walk_forward", "execution_mode"}
    incomplete = evaluate_native_trade_packet({"symbol": "BTC/USDT"})["safe"] is False
    review = native_research_review({}, {})
    golden = {
        "symbol": "BTC/USDT", "asset": "crypto", "exchange": "binance", "timeframe": "1h",
        "market_timestamp": datetime.now(timezone.utc).isoformat(),
        "trade_plan": {"side": "buy", "entry_price": 100.0, "stop_loss_price": 98.0, "take_profit_price": 104.0},
        "deterministic_rules": {"self_check": "ok"},
        "price_context": {"latest_close": 100.0, "bars": 300, "spread_bps": 2.0},
        "data_quality": {"safe": True, "reasons": []}, "adaptive_model": {"status": "OK"}, "ml_signal": {"signal": 0.5},
    }
    valid_passes = evaluate_native_trade_packet(golden)["safe"] is True
    inverted = dict(golden, trade_plan={"side": "buy", "entry_price": 100.0, "stop_loss_price": 104.0, "take_profit_price": 98.0})
    stale_future = dict(golden, market_timestamp="2999-01-01T00:00:00+00:00")
    unsafe_vetoed = (
        evaluate_native_trade_packet(inverted)["safe"] is False
        and evaluate_native_trade_packet(stale_future)["safe"] is False
    )
    negation_ok = compose_strategy_spec(prompt="Never go long BTC, short only on 1h", name="n")["direction"] == "SHORT"
    ok = valid_passes and unsafe_vetoed and negation_ok and keys.issubset(draft) and draft["strategy_family"] in _ALLOWED_FAMILIES and draft["execution_mode"] == "PAPER_SHADOW_ONLY" and incomplete and review["execution_authority"] is False and review["network_calls"] == 0
    return {"status": "PASS" if ok else "FAIL", "engine": "atlas-native-ai-v1", "network_calls": 0,
        "external_model_credentials_required": False, "checks": {"strategy_composer": keys.issubset(draft),
        "incomplete_trade_fails_closed": incomplete, "research_is_advisory": review["execution_authority"] is False,
        "valid_trade_passes": valid_passes, "unsafe_trades_vetoed": unsafe_vetoed, "negated_direction_not_misread": negation_ok}}


async def dual_ai_trade_safety_review(trade_packet: dict[str, Any]) -> dict[str, Any]:
    """Legacy call signature; executes the local veto without provider calls."""
    return await native_trade_safety_review(trade_packet)


async def dual_ai_research_review(research: dict[str, Any], intelligence: dict[str, Any]) -> dict[str, Any]:
    review = native_research_review(research, intelligence)
    return {"mode": "ATLAS_NATIVE_RESEARCH_REVIEW", "providers": {"atlas_native": review}, "errors": {},
        "execution_authority": False, "network_calls": 0}
