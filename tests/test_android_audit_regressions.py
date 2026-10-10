from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_customer_dashboard_does_not_seed_balances_pnl_or_fake_equity_history():
    source = (ROOT / "app/src/main/java/com/atlas/trading/ui/UserDashboardScreen.kt").read_text()
    assert 'totalBalance = "Unavailable"' in source
    assert 'activeTrades = "Unavailable"' in source
    assert 'profit24hPct = "Unavailable"' in source
    assert "generate30DayEquityData" not in source
    assert "1250.00" not in source
    assert "3.5" not in source


def test_customer_dashboard_does_not_embed_sample_market_quotes_or_activity():
    source = (ROOT / "app/src/main/java/com/atlas/trading/ui/UserDashboardScreen.kt").read_text()
    for sample in ("1.0852", "1.2940", "154.20", "2,354.20", "3,480", "78.60", "+0.05", "+25 pips"):
        assert sample not in source, f"sample market/activity value remains: {sample}"
    assert "verified provider data is received" in source
    assert "verified account activity is received" in source


def test_customer_identity_is_not_hardcoded():
    dashboard = (ROOT / "app/src/main/java/com/atlas/trading/ui/UserDashboardScreen.kt").read_text()
    settings = (ROOT / "app/src/main/java/com/atlas/trading/ui/ModernSettingsScreen.kt").read_text()
    activity = (ROOT / "app/src/main/java/com/atlas/trading/MainActivity.kt").read_text()
    for source in (dashboard, settings, activity):
        assert "Alice Henderson" not in source
        assert "alice.henderson@atlas.trading" not in source
        assert 'userName = "Alice"' not in source


def test_main_android_resources_do_not_ship_development_backend_url():
    source = (ROOT / "app/src/main/res/values/strings.xml").read_text()
    assert "ais-dev" not in source
    assert "europe-west2.run.app" not in source


def test_release_build_requires_explicit_approved_https_backend_host():
    source = (ROOT / "app/build.gradle.kts").read_text()
    workflow = (ROOT / ".github/workflows/android-release.yml").read_text()
    assert "ATLAS_PROD_BACKEND_URL" in source
    assert "ATLAS_PROD_BACKEND_HOST" in source
    assert 'parsedBackend?.scheme == "https"' in source
    assert "actualHost == approvedBackendHost" in source
    assert "vars.ATLAS_PROD_BACKEND_URL" in workflow
    assert "vars.ATLAS_PROD_BACKEND_HOST" in workflow


def test_strategy_replay_is_disclosed_as_synthetic_not_a_real_backtest():
    source = (ROOT / "app/src/main/java/com/atlas/trading/ui/StrategyReplayEngine.kt").read_text()
    assert "SYNTHETIC SCENARIO SIMULATION" in source
    assert "not real exchange candles, verified historical backtests, or customer P&L" in source
