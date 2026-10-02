"""Pydantic request/response models for the HTTP API (extracted from main.py in 3.10.46; no behaviour change)."""
from __future__ import annotations
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class MarketRequest(BaseModel):
    asset: str = Field(default="crypto", pattern="^(crypto|forex|commodity)$")
    symbol: str = "BTC/USDT:USDT"
    exchange: str = "binance"
    timeframe: str = "1h"
    days: int = Field(default=365, ge=30, le=1500)


class CustomerBotStartRequest(MarketRequest):
    risk_fraction: float | None = Field(default=None, gt=0, le=0.02)
    autonomous: bool = True
    interval_seconds: int = Field(default=900, ge=60, le=86400)
    strategy_candidate_id: int | None = Field(default=None, gt=0)


class SmartTradeRequest(BaseModel):
    symbol: str = Field(min_length=2, max_length=80)
    exchange: str = Field(default="bybit", min_length=2, max_length=50)
    side: str = Field(pattern="^(BUY|SELL)$")
    entry_price: float = Field(gt=0)
    quantity: float = Field(gt=0)
    stop_loss_price: float = Field(gt=0)
    take_profit_1: float = Field(gt=0)
    take_profit_2: float = Field(default=0, ge=0)
    take_profit_3: float = Field(default=0, ge=0)
    trailing_stop_pct: float = Field(default=0, ge=0, le=20)
    breakeven_at_r: float = Field(default=1.0, ge=0, le=10)
    notes: str = Field(default="", max_length=1000)


class DcaBotRequest(BaseModel):
    symbol: str = Field(min_length=2, max_length=80)
    exchange: str = Field(default="bybit", min_length=2, max_length=50)
    side: str = Field(default="LONG", pattern="^(LONG|SHORT)$")
    initial_quote: float = Field(gt=0)
    safety_order_quote: float = Field(gt=0)
    max_safety_orders: int = Field(default=3, ge=1, le=20)
    deviation_pct: float = Field(default=1.0, gt=0, le=50)
    volume_scale: float = Field(default=1.5, ge=1, le=5)
    step_scale: float = Field(default=1.25, ge=1, le=5)
    take_profit_pct: float = Field(default=2.0, gt=0, le=100)
    stop_loss_pct: float = Field(default=5.0, gt=0, le=100)
    trailing_take_profit_pct: float = Field(default=0, ge=0, le=50)


class AlertRequest(BaseModel):
    alert_type: str = Field(default="PRICE", pattern="^(PRICE|TRADE|RISK|SYSTEM)$")
    symbol: str = Field(default="", max_length=80)
    threshold: float = Field(default=0, ge=0)
    condition: str = Field(default="ABOVE", pattern="^(ABOVE|BELOW|CROSSES)$")
    channel: str = Field(default="IN_APP", pattern="^(IN_APP|EMAIL)$")
    message: str = Field(default="", max_length=500)


class StrategyLabRequest(BaseModel):
    name: str = Field(default="Atlas Candidate", min_length=2, max_length=120)
    prompt: str = Field(min_length=20, max_length=4000)
    asset: str = Field(default="crypto", pattern="^(crypto|forex|commodity)$")
    symbol: str = Field(default="BTC/USDT:USDT", min_length=2, max_length=80)
    exchange: str = Field(default="binance", min_length=2, max_length=50)
    timeframe: str = Field(default="1h", pattern="^(5m|15m|30m|1h|4h|1d)$")
    days: int = Field(default=365, ge=60, le=1500)


class ExecutorCreateRequest(BaseModel):
    kind: str = Field(pattern="^(POSITION|DCA|TWAP)$")
    asset: str = Field(default="crypto", pattern="^(crypto|forex|commodity)$")
    symbol: str = Field(min_length=2, max_length=80)
    exchange: str = Field(default="binance", min_length=2, max_length=50)
    timeframe: str = Field(default="1h")
    side: str = Field(pattern="^(buy|sell)$")
    total_quantity: float = Field(gt=0)
    slices: int = Field(default=1, ge=1, le=200)
    interval_seconds: int = Field(default=60, ge=1, le=86400)
    max_slippage_bps: float = Field(default=25, ge=0, le=500)
    stop_loss_price: float | None = Field(default=None, gt=0)
    take_profit_price: float | None = Field(default=None, gt=0)
    mode: str = Field(default="PAPER", pattern="^(PAPER|LIVE)$")


class StrategyBuilderRequest(BaseModel):
    prompt: str = Field(min_length=20, max_length=4000)
    name: str = Field(default="Atlas AI Strategy", min_length=2, max_length=120)
    # Market fields are optional and default to the Strategy Lab defaults so existing
    # clients that only send prompt/name keep working.
    asset: str = Field(default="crypto", pattern="^(crypto|forex|commodity)$")
    symbol: str = Field(default="BTC/USDT:USDT", min_length=2, max_length=80)
    exchange: str = Field(default="binance", min_length=2, max_length=50)
    timeframe: str = Field(default="1h", pattern="^(5m|15m|30m|1h|4h|1d)$")


class GridBotRequest(BaseModel):
    symbol: str = Field(min_length=2, max_length=80)
    exchange: str = Field(default="bybit", min_length=2, max_length=50)
    grid_type: str = Field(default="NEUTRAL", pattern="^(LONG|NEUTRAL|SHORT)$")
    lower_price: float = Field(gt=0)
    upper_price: float = Field(gt=0)
    levels: int = Field(default=20, ge=2, le=200)
    arithmetic: bool = False
    quote_per_grid: float = Field(gt=0)
    take_profit_pct: float = Field(default=0, ge=0, le=100)
    stop_loss_pct: float = Field(default=0, ge=0, le=100)
    trailing_stop_pct: float = Field(default=0, ge=0, le=50)


class WebhookCreateRequest(BaseModel):
    name: str = Field(default="TradingView", min_length=2, max_length=100)


class ConnectorTestRequest(BaseModel):
    exchange: str = Field(min_length=2, max_length=50)
    market_type: str = Field(default="spot", pattern="^(spot|swap|future)$")


class ArbitrageLeg(BaseModel):
    symbol: str = Field(min_length=2, max_length=80)
    side: str = Field(pattern="^(BUY|SELL)$")
    base_asset: str = Field(min_length=2, max_length=20, pattern=r"^[A-Za-z0-9_-]+$")
    quote_asset: str = Field(min_length=2, max_length=20, pattern=r"^[A-Za-z0-9_-]+$")
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    bid_qty: float = Field(default=0, ge=0)
    ask_qty: float = Field(default=0, ge=0)


class DerivPreflightRequest(BaseModel):
    currency: str | None = Field(default=None, max_length=10)


class DerivConnectRequest(BaseModel):
    account_id: str = Field(min_length=3, max_length=80)
    api_token: str = Field(min_length=20, max_length=1024)


class DerivTradeRequest(BaseModel):
    contract_type: str = Field(min_length=2, max_length=40)
    underlying_symbol: str = Field(min_length=2, max_length=40)
    amount: float = Field(gt=0)
    duration: int | None = Field(default=None, gt=0, le=86400)
    duration_unit: str | None = Field(default=None, max_length=4)
    basis: str = Field(default="stake", pattern="^(stake|payout)$")
    contract_category: str | None = Field(default=None, max_length=30)
    barrier: str | None = Field(default=None, max_length=40)
    currency: str | None = Field(default=None, max_length=10)


class BinanceArbitrageLiveRequest(BaseModel):
    legs: list[ArbitrageLeg] = Field(min_length=3, max_length=3)
    start_quote: float = Field(gt=0)
    start_asset: str = Field(min_length=2, max_length=20, pattern=r"^[A-Za-z0-9_-]+$")
    confirmation: str = Field(default="", max_length=64)


class ArbitragePaperRequest(BaseModel):
    legs: list[ArbitrageLeg] = Field(min_length=3, max_length=3)
    start_quote: float = Field(gt=0)
    start_asset: str = Field(min_length=2, max_length=20, pattern=r"^[A-Za-z0-9_-]+$")


class ExecutionPlanRequest(BaseModel):
    side: str = Field(pattern="^(buy|sell)$")
    quantity: float = Field(gt=0)
    best_price: float = Field(gt=0)
    levels: list[dict[str, float]] = Field(min_length=1, max_length=100)
    maker_fee_bps: float = Field(default=0, ge=0, le=500)
    taker_fee_bps: float = Field(default=0, ge=0, le=500)
    latency_ms: float = Field(default=0, ge=0, le=60000)
    short_term_vol_bps_per_s: float = Field(default=0, ge=0, le=10000)
    max_adverse_bps: float = Field(default=50, gt=0, le=5000)
    urgency: float = Field(default=0.5, ge=0, le=1)


class BotActionRequest(BaseModel):
    action: str = Field(pattern="^(START|STOP)$")


class TrainRequest(MarketRequest):
    min_train: int = Field(default=800, ge=200, le=5000)
    folds: int = Field(default=5, ge=2, le=10)


class ExecuteRequest(MarketRequest):
    side: str = Field(pattern="^(buy|sell)$")
    quantity: float = Field(gt=0)
    price: float = Field(default=0, ge=0)
    score: float = 0.0
    long_probability: float = 0.0
    short_probability: float = 0.0
    flat_probability: float = 0.0
    signal_timestamp: str | None = None
    request_id: str | None = Field(default=None, min_length=8, max_length=80)
    force_paper: bool = True
    stop_loss_price: float | None = Field(default=None, gt=0)
    take_profit_price: float | None = Field(default=None, gt=0)
    strategy: str = "manual-v1"
    demo_forex: bool = False


class LiveEnableRequest(BaseModel):
    confirmation: str


class WithdrawalCreate(BaseModel):
    request_id: str = Field(min_length=3, max_length=120)
    account_ref: str = Field(min_length=1, max_length=120)
    amount: float = Field(gt=0)
    currency: str = Field(min_length=2, max_length=20)
    destination_masked: str = Field(min_length=3, max_length=180)
    risk_score: float = Field(default=0.0, ge=0, le=1)
    risk_flags: list[str] = Field(default_factory=list, max_length=20)
    destination: str | None = Field(default=None, min_length=3, max_length=500)
    destination_tag: str | None = Field(default=None, max_length=120)
    network: str | None = Field(default=None, max_length=40)
    provider: str = Field(default="", max_length=40)

    @field_validator("amount")
    @classmethod
    def _amount_max_six_decimals(cls, v: float) -> float:
        exponent = Decimal(str(v)).as_tuple().exponent
        if not isinstance(exponent, int) or exponent < -6:
            raise ValueError("amount supports at most 6 decimal places")
        return v


class CustomerWithdrawalCreate(BaseModel):
    amount: float = Field(gt=0)
    destination: str = Field(min_length=30, max_length=50)
    destination_tag: str | None = Field(default=None, max_length=120)
    network: str = Field(default="TRON", min_length=3, max_length=40)

    @field_validator("amount")
    @classmethod
    def _amount_max_six_decimals(cls, v: float) -> float:
        exponent = Decimal(str(v)).as_tuple().exponent
        if not isinstance(exponent, int) or exponent < -6:
            raise ValueError("amount supports at most 6 decimal places")
        return v


class WithdrawalDecision(BaseModel):
    admin_id: str = Field(min_length=2, max_length=120)
    reason: str = Field(default="", max_length=500)


class WithdrawalExecuteRequest(BaseModel):
    operator_id: str = Field(min_length=2, max_length=120)
    signature: str | None = None


class WithdrawalReconcileRequest(BaseModel):
    operator_id: str = Field(min_length=2, max_length=120)


class ModelRollbackRequest(BaseModel):
    asset: str = Field(default="crypto", pattern="^(crypto|forex|commodity)$")
    symbol: str = Field(default="BTC/USDT:USDT", min_length=3, max_length=80)
    exchange: str = Field(default="binance", min_length=2, max_length=50)
    timeframe: str = Field(default="1h", min_length=2, max_length=10)


class PlanChangeRequest(BaseModel):
    plan: str = Field(pattern="^(free|starter|pro|elite)$")
    interval: str = Field(default="monthly", pattern="^(monthly|annual)$")


class ReferralCodeRequest(BaseModel):
    code: str | None = Field(default=None, min_length=4, max_length=40)


class CostEventRequest(BaseModel):
    customer_id: int | None = None
    category: str = Field(min_length=2, max_length=40)
    provider: str = Field(default="", max_length=60)
    amount: float = Field(ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=10)
    reference: str = Field(default="", max_length=180)


class RevenueEventRequest(BaseModel):
    customer_id: int | None = None
    subscription_id: int | None = None
    provider: str = Field(default="manual", max_length=30)
    provider_reference: str = Field(min_length=2, max_length=180)
    gross_amount: float = Field(ge=0)
    refunds: float = Field(default=0, ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=10)


class BinanceSubAccountProvisionRequest(BaseModel):
    customer_id: int = Field(gt=0)
    tag: str = Field(min_length=1, max_length=31)


class AdminLoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)


class CustomerCredentials(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    referral_code: str | None = Field(default=None, min_length=4, max_length=40)


class OtpSendRequest(BaseModel):
    email: str | None = None
    phone: str | None = None
    create_user: bool = False
    purpose: str = Field(default="login", pattern="^(login|withdrawal|destination_verification)$")
    destination: str | None = Field(default=None, min_length=30, max_length=50)
    amount: float | None = Field(default=None, gt=0)
    destination_tag: str | None = Field(default=None, max_length=120)
    network: str = Field(default="TRON", min_length=3, max_length=40)


class OtpVerifyRequest(BaseModel):
    email: str | None = None
    phone: str | None = None
    token: str = Field(min_length=6, max_length=8)
    purpose: str = Field(default="login", pattern="^(login|withdrawal|destination_verification)$")
    destination: str | None = Field(default=None, min_length=30, max_length=50)
    amount: float | None = Field(default=None, gt=0)
    destination_tag: str | None = Field(default=None, max_length=120)
    network: str = Field(default="TRON", min_length=3, max_length=40)
    otp_intent_token: str | None = Field(default=None, min_length=20, max_length=1200)


class FundingWebhook(BaseModel):
    customer_auth_user_id: str = Field(min_length=10, max_length=120)
    provider: str = Field(min_length=2, max_length=50)
    provider_reference: str = Field(min_length=2, max_length=180)
    amount: float = Field(gt=0)
    currency: str = Field(min_length=2, max_length=20)
    status: str = Field(default="CONFIRMED", pattern="^(PENDING|CONFIRMED|FAILED)$")
    metadata: dict = Field(default_factory=dict)


class MfaChallengeRequest(BaseModel):
    factor_id: str = Field(min_length=10, max_length=80)


class MfaVerifyRequest(BaseModel):
    factor_id: str = Field(min_length=10, max_length=80)
    challenge_id: str = Field(min_length=10, max_length=80)
    code: str = Field(pattern=r"^\d{6}$")


class CustomerOandaConnectRequest(BaseModel):
    account_id: str = Field(min_length=3, max_length=80)
    api_token: str = Field(min_length=20, max_length=512)
    practice: bool = True
