from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import settings
from .native_ai import NativeAIError, compose_strategy_spec
from .strategy_evidence import evidence_for


ALLOWED_TIMEFRAMES = {"5m", "15m", "30m", "1h", "4h", "1d"}
ALLOWED_ASSETS = {"crypto", "forex", "commodity"}
ALLOWED_DIRECTIONS = {"LONG", "SHORT", "BOTH", "AUTO"}
ALLOWED_STRATEGY_FAMILIES = {"TREND", "MOMENTUM", "BREAKOUT", "MEAN_REVERSION", "ENSEMBLE"}


class StrategyCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(min_length=2, max_length=60)
    rule: str = Field(min_length=3, max_length=500)
    timeframe: str | None = Field(default=None, max_length=10)

    @field_validator("timeframe")
    @classmethod
    def validate_timeframe(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe: {value}")
        return value


class AIStrategySpec(BaseModel):
    """Native AI output contract. It describes a strategy but grants no execution authority."""

    model_config = ConfigDict(extra="forbid")
    version: Literal["atlas-strategy-v1"] = "atlas-strategy-v1"
    title: str = Field(min_length=2, max_length=120)
    summary: str = Field(min_length=10, max_length=1000)
    asset: Literal["crypto", "forex", "commodity"]
    symbols: list[str] = Field(min_length=1, max_length=10)
    timeframes: list[str] = Field(min_length=1, max_length=5)
    direction: Literal["LONG", "SHORT", "BOTH", "AUTO"]
    strategy_family: Literal["TREND", "MOMENTUM", "BREAKOUT", "MEAN_REVERSION", "ENSEMBLE"]
    entry_conditions: list[StrategyCondition] = Field(min_length=1, max_length=12)
    exit_conditions: list[StrategyCondition] = Field(min_length=1, max_length=8)
    risk_per_trade: float = Field(gt=0, le=0.05)
    stop_loss_atr: float | None = Field(default=None, gt=0, le=10)
    take_profit_rr: float | None = Field(default=None, gt=0, le=10)
    sessions: list[str] = Field(default_factory=list, max_length=8)
    assumptions: list[str] = Field(default_factory=list, max_length=10)
    validation_warnings: list[str] = Field(default_factory=list, max_length=12)
    requires_backtest: Literal[True] = True
    requires_walk_forward: Literal[True] = True
    execution_mode: Literal["PAPER_SHADOW_ONLY"] = "PAPER_SHADOW_ONLY"
    implementation_status: Literal["ATLAS_SUPPORTED_FAMILY"] = "ATLAS_SUPPORTED_FAMILY"

    @field_validator("timeframes")
    @classmethod
    def validate_timeframes(cls, values: list[str]) -> list[str]:
        if any(v not in ALLOWED_TIMEFRAMES for v in values):
            raise ValueError(f"Unsupported timeframe; allowed: {sorted(ALLOWED_TIMEFRAMES)}")
        return list(dict.fromkeys(values))

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, values: list[str]) -> list[str]:
        cleaned = [str(v).strip() for v in values if str(v).strip()]
        if not cleaned:
            raise ValueError("At least one symbol is required")
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def validate_risk_contract(self) -> "AIStrategySpec":
        if self.risk_per_trade > settings.risk_per_trade:
            self.validation_warnings.append(
                f"Requested AI risk was capped at the platform maximum of {settings.risk_per_trade:.4f}."
            )
            self.risk_per_trade = settings.risk_per_trade
        return self


def _apply_market_context(spec: AIStrategySpec, *, asset: str | None = None, symbol: str | None = None, timeframe: str | None = None) -> AIStrategySpec:
    """Bind an AI draft to the market selected by the user before persistence/backtest."""
    warnings = list(spec.validation_warnings)
    data = spec.model_dump()
    if asset and spec.asset != asset:
        warnings.append(f"AI asset overridden to the selected market asset: {asset}.")
        data["asset"] = asset
    if symbol and (not spec.symbols or symbol not in spec.symbols):
        warnings.append(f"AI symbol overridden to the selected market symbol: {symbol}.")
        data["symbols"] = [symbol]
    elif symbol:
        data["symbols"] = [symbol]
    if timeframe:
        condition_timeframes = [
            str(condition.get("timeframe") or "")
            for key in ("entry_conditions", "exit_conditions")
            for condition in data.get(key, [])
        ]
        requested_different_timeframes = (
            any(value != timeframe for value in spec.timeframes)
            or any(value and value != timeframe for value in condition_timeframes)
        )
        if requested_different_timeframes:
            warnings.append(
                f"Selected market timeframe {timeframe} overrides the draft's multi-timeframe setup; "
                "all conditions were constrained to this timeframe so validation uses a consistent dataset."
            )
        elif not spec.timeframes or timeframe not in spec.timeframes:
            warnings.append(f"AI timeframe overridden to the selected market timeframe: {timeframe}.")
        data["timeframes"] = [timeframe]
        for key in ("entry_conditions", "exit_conditions"):
            data[key] = [
                {**condition, "timeframe": timeframe}
                for condition in data.get(key, [])
            ]
    data["validation_warnings"] = warnings[:12]
    return AIStrategySpec.model_validate(data)


async def generate_strategy_draft(*, prompt: str, name: str, asset: str | None = None, symbol: str | None = None, timeframe: str | None = None) -> dict[str, Any]:
    """Generate an auditable local strategy template; no hosted model/network call occurs."""
    try:
        raw = compose_strategy_spec(prompt=prompt, name=name, asset=asset, symbol=symbol,
                                    timeframe=timeframe, max_risk_per_trade=settings.risk_per_trade)
        spec = AIStrategySpec.model_validate(raw)
        spec = _apply_market_context(spec, asset=asset, symbol=symbol, timeframe=timeframe)
    except Exception as exc:
        raise NativeAIError(f"Atlas Native AI could not create a valid strategy draft: {exc}") from exc
    family = {"TREND":"trend", "MOMENTUM":"momentum", "BREAKOUT":"breakout",
              "MEAN_REVERSION":"mean_reversion", "ENSEMBLE":"ensemble"}[spec.strategy_family]
    return {
        "strategy": spec.model_dump(),
        "evidence": evidence_for(family),
        "ai": {"provider":"atlas_native", "model":"atlas-native-strategy-v1",
               "fallback_used":False, "provider_errors":{}, "execution_authority":False,
               "requires_backtest":True, "requires_walk_forward":True, "network_calls":0},
    }
