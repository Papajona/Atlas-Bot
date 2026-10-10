from pathlib import Path

from app.execution import customer_paper_execution_blocked


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_customer_paper_execution_guard_behaviour():
    assert customer_paper_execution_blocked(101, True) is True
    assert customer_paper_execution_blocked(101, False) is False
    assert customer_paper_execution_blocked(None, True) is False

    source = _source("app/execution.py")
    assert "Customer paper execution requires an isolated simulation ledger" in source
    assert 'and mode == "LIVE"' in source
    assert 'str(trade.mode or "").upper() == "LIVE"' in source


def test_live_customer_equity_is_recomputed_from_ledger_and_open_positions():
    source = _source("app/execution.py")
    assert "async def _refresh_live_customer_equity" in source
    assert 'balance["available"] + balance["trading_reserved"]' in source
    assert "account.equity = max(0.0, account.cash_equity + unrealized)" in source


def test_rbac_inactive_assignment_blocks_allowlist_fallback():
    source = _source("app/admin_rbac.py")
    assert "select(AdminRole.role, AdminRole.active)" in source
    assert "if rows:" in source
    assert "if uid in allowed:" in source


def test_oos_gate_missing_required_metrics_fails_closed():
    source = _source("app/research_validation.py")
    assert 'sharpe is not None and sharpe >= float(min_sharpe)' in source
    assert 'max_dd is not None and max_dd >= float(max_drawdown)' in source
    assert 'trades is not None and trades >= int(min_trades)' in source
    assert '("oos_total_return" in oos or "total_return" in oos)' in source


def test_walk_forward_fold_returns_are_shifted():
    source = _source("app/trading_core.py")
    assert "fold_position = np.r_[0, sig[:-1]]" in source
    assert "fold_net = fold_position * fold_ret" in source


def test_customer_live_venue_is_locked_to_binance_spot():
    config = _source("app/config.py")
    execution = _source("app/customer_binance_execution.py")
    assert 'customer_live_exchange: str = "binance"' in config
    assert 'customer_live_market_type: str = "spot"' in config
    assert 'Customer live venue is not configured for Binance' in execution
    assert 'Customer live market type must be spot' in execution


def test_funding_webhook_requires_fresh_timestamp_bound_signature():
    source = _source("app/main.py")
    assert "x_funding_timestamp" in source
    assert "funding_webhook_max_skew_seconds" in _source("app/config.py")
    assert 'signed = str(timestamp).strip().encode() + b"." + raw_body' in source


def test_customer_cash_reserve_does_not_treat_a_position_flip_as_reduce_only():
    source = _source("app/execution.py")
    start = source.index("reserved_cash = 0.0")
    end = source.index('if mode == "PAPER":', start)
    block = source[start:end]
    assert "opposing_qty = sum(" in block
    assert "quantity <= opposing_qty + 1e-9" in block
    assert "if not reducing:" in block
