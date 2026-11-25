"""Runtime configuration guard tests for API hardening."""
import pytest

from app.config import Settings


def _settings():
    return Settings(environment="test")


def test_production_requires_admin_metrics_and_disables_api_docs():
    with pytest.raises(ValueError, match="METRICS_REQUIRE_ADMIN must remain true"):
        _settings().model_copy(update={
            "environment": "production",
            "metrics_require_admin": False,
        })._production_security_requirements()

    with pytest.raises(ValueError, match="DOCS_ENABLED must remain false"):
        _settings().model_copy(update={
            "environment": "production",
            "docs_enabled": True,
        })._production_security_requirements()


def test_paper_staging_market_scanner_is_staging_only_and_disabled_by_default():
    assert _settings().paper_staging_market_scanner_enabled is False
    # Keep the staging API's unrelated startup requirements satisfied so this
    # assertion isolates the market-scanner environment boundary.
    _settings().model_copy(update={
        "environment": "staging",
        "paper_staging_market_scanner_enabled": True,
        "supabase_url": "https://atlas-test.supabase.co",
        "supabase_anon_key": "test-supabase-anon-key",
        "funding_webhook_secret": "test-funding-webhook-secret",
        "forwarded_allow_ips": "127.0.0.1",
        "model_signing_public_key": "test-model-signing-public-key",
    })._production_security_requirements()
    with pytest.raises(ValueError, match="PAPER_STAGING_MARKET_SCANNER_ENABLED may only be true in staging"):
        _settings().model_copy(update={
            "environment": "production",
            "paper_staging_market_scanner_enabled": True,
        })._production_security_requirements()


def test_request_body_limit_must_be_non_negative_and_enabled_in_staging_and_production():
    with pytest.raises(ValueError, match="MAX_REQUEST_BODY_BYTES must be >= 0"):
        _settings().model_copy(update={
            "max_request_body_bytes": -1,
        })._production_security_requirements()

    with pytest.raises(ValueError, match="MAX_REQUEST_BODY_BYTES must be > 0"):
        _settings().model_copy(update={
            "environment": "staging",
            "max_request_body_bytes": 0,
        })._production_security_requirements()


def test_development_or_test_may_disable_request_body_limit():
    _settings().model_copy(update={
        "max_request_body_bytes": 0,
    })._production_security_requirements()

@pytest.mark.parametrize(
    "field",
    ["max_slippage_bps", "max_spread_bps"],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -1.0])
def test_market_quality_ceilings_must_be_finite_and_non_negative(field, value):
    with pytest.raises(ValueError, match="MAX_SLIPPAGE_BPS and MAX_SPREAD_BPS"):
        Settings(environment="test", **{field: value})


def test_zero_market_quality_ceilings_are_valid():
    settings = Settings(
        environment="test",
        max_slippage_bps=0.0,
        max_spread_bps=0.0,
    )
    assert settings.max_slippage_bps == 0.0
    assert settings.max_spread_bps == 0.0

@pytest.mark.parametrize(
    "field",
    [
        "initial_equity",
        "max_leverage",
        "risk_per_trade",
        "max_drawdown",
        "daily_loss_limit",
        "max_notional_usd",
        "max_position_notional_usd",
        "max_total_exposure_usd",
    ],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -1.0])
def test_position_sizing_and_exposure_limits_reject_non_finite_or_negative(field, value):
    with pytest.raises(ValueError):
        Settings(environment="test", **{field: value})


@pytest.mark.parametrize("field", ["max_drawdown", "daily_loss_limit"])
def test_loss_limits_must_remain_fractional(field):
    with pytest.raises(ValueError, match="finite fractions"):
        Settings(environment="test", **{field: 1.0})


def test_zero_limits_are_explicit_trade_halts_not_unbounded_values():
    settings = Settings(
        environment="test",
        max_leverage=0.0,
        risk_per_trade=0.0,
        max_drawdown=0.0,
        daily_loss_limit=0.0,
        max_notional_usd=0.0,
        max_position_notional_usd=0.0,
        max_total_exposure_usd=0.0,
        max_open_positions=0,
    )
    assert settings.max_leverage == 0.0
    assert settings.risk_per_trade == 0.0
    assert settings.max_open_positions == 0

def test_initial_equity_and_open_position_count_cannot_disable_validation():
    with pytest.raises(ValueError, match="INITIAL_EQUITY"):
        Settings(environment="test", initial_equity=0.0)
    with pytest.raises(ValueError, match="MAX_OPEN_POSITIONS"):
        Settings(environment="test", max_open_positions=-1)

