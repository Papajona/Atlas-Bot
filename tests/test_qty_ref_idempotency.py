"""Idempotency-key canonicalisation for fill-derived ledger references.

Runs without the app's heavy dependencies: if app.execution cannot be imported (missing
packages), the real _qty_ref source is extracted from app/execution.py via ast.
"""
import ast
import math
import pathlib
from decimal import Decimal, ROUND_HALF_EVEN


def _load_qty_ref():
    try:
        from app.execution import _qty_ref
        return _qty_ref
    except (ModuleNotFoundError, ImportError):
        src = pathlib.Path(__file__).resolve().parents[1].joinpath("app", "execution.py").read_text()
        tree = ast.parse(src)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_qty_ref")
        ns = {"Decimal": Decimal, "ROUND_HALF_EVEN": ROUND_HALF_EVEN}
        exec(compile(ast.Module([fn], []), "execution.py", "exec"), ns)
        return ns["_qty_ref"]


qty_ref = _load_qty_ref()


def test_one_ulp_drift_does_not_change_key():
    for k in range(1, 200_000):
        a = k / 100_000_000
        assert qty_ref(math.nextafter(a, 0.0)) == qty_ref(a) == qty_ref(math.nextafter(a, 1.0))


def test_distinct_ledger_precision_quantities_never_share_a_key():
    # The customer ledger stores quantities at 6 decimal places; every distinct
    # ledger-representable positive quantity in this range must retain its key.
    keys = {qty_ref(k / 1_000_000) for k in range(1, 200_000)}
    assert len(keys) == 199_999


def test_old_collision_is_gone():
    assert int(0.0000011 * 1_000_000) == int(0.0000019 * 1_000_000)
    assert qty_ref(0.0000011) != qty_ref(0.0000019)


def test_none_and_zero():
    assert qty_ref(None) == qty_ref(0) == "0.000000000"


def test_negative_zero_and_decimal_inputs_are_canonical():
    assert qty_ref(-0.0) == "0.000000"
    assert qty_ref(Decimal("0.1234564")) == "0.123456"
    assert qty_ref(Decimal("0.1234566")) == "0.123457"


def test_key_precision_matches_ledger_precision():
    assert qty_ref(Decimal("0.1234561")) == qty_ref(Decimal("0.1234564"))
    assert qty_ref(Decimal("0.1234564")) != qty_ref(Decimal("0.1234574"))
