from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_binance_testnet_harness_has_hard_production_boundary():
    source = (ROOT / "scripts/binance_spot_testnet_acceptance.py").read_text()
    assert 'ATLAS_BINANCE_TESTNET' in source
    assert 'testnet.binance.vision' in source
    assert 'sandbox=True' in source
    assert 'enable_withdrawals=False' in source
    assert 'universal_transfer=False' in source


def test_binance_spot_harness_uses_spot_stop_loss_not_derivatives_reduce_only():
    source = (ROOT / "scripts/binance_spot_testnet_acceptance.py").read_text()
    assert '"STOP_LOSS"' in source
    assert '"stopPrice"' in source
    assert 'reduceOnly' not in source


def test_binance_spot_harness_reconciles_after_flatten():
    source = (ROOT / "scripts/binance_spot_testnet_acceptance.py").read_text()
    assert 'fetch_balance' in source
    assert 'remaining_base' in source
    assert 'Residual Spot base balance' in source
