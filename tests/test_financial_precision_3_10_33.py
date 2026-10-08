from pathlib import Path
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[1]


def test_money_columns_are_numeric_storage_not_float():
    source = (ROOT / "app/db.py").read_text()
    # Custodial wallet mirrors and the authoritative customer ledger use Decimal/Numeric,
    # while broader trading models intentionally retain the legacy-compatible FinancialNumeric.
    assert 'available_balance: Mapped[Decimal] = mapped_column(Numeric(38, 18)' in source
    assert 'locked_balance: Mapped[Decimal] = mapped_column(Numeric(38, 18)' in source
    assert 'available: Mapped[Decimal] = mapped_column(Numeric(38, 6)' in source
    assert 'trading_reserved: Mapped[Decimal] = mapped_column(Numeric(38, 6)' in source
    assert 'withdrawal_reserved: Mapped[Decimal] = mapped_column(Numeric(38, 6)' in source


def test_default_and_live_database_safety_contract():
    cfg = (ROOT / "app/config.py").read_text()
    main = (ROOT / "app/main.py").read_text()
    assert 'database_url: str = "sqlite+aiosqlite:///./data/trading.db"' in cfg
    assert 'PostgreSQL is required outside development' in main


def test_withdrawal_digest_uses_canonical_decimal_text():
    from app.withdrawal_security import canonical_proposal
    assert b'"amount":"1.23"' in canonical_proposal(request_id="x", amount=Decimal("1.2300"), currency="USDT", destination="T123")
    assert canonical_proposal(request_id="x", amount=0.0, currency="USDT", destination="T123") == canonical_proposal(request_id="x", amount=Decimal("-0"), currency="USDT", destination="T123")


def test_withdrawal_velocity_has_no_row_cap():
    source = (ROOT / "app/withdrawal_risk.py").read_text()
    assert '.limit(100)' not in source
    assert 'func.sum' in source


def test_usdt_tron_no_custom_keccak_tables_remain():
    source = (ROOT / "app/usdt_tron.py").read_text()
    assert '_ROT' not in source and '_RC' not in source
    req = (ROOT / "requirements.txt").read_text()
    assert 'bip-utils==2.12.2' in req
    assert 'pycryptodomex==3.23.0' not in req
    assert 'from bip_utils import Bip44' in source
    assert '_derive_path' not in source


def test_sensitive_plaintext_fails_closed_outside_development():
    crypto = (ROOT / "app/crypto.py").read_text()
    assert 'environment.lower() in {"production", "staging"}' in crypto
    assert 'Unencrypted sensitive database value detected outside development' in crypto
