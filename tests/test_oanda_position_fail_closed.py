from app.execution import _parse_oanda_position_units


def test_oanda_position_units_parse_explicit_zero_and_signed_values():
    row = {"long": {"units": "0"}, "short": {"units": "-250"}}
    assert _parse_oanda_position_units(row, "long") == 0
    assert _parse_oanda_position_units(row, "short") == -250


def test_oanda_position_units_reject_missing_malformed_and_nonfinite_values():
    assert _parse_oanda_position_units({"long": {}, "short": {"units": "0"}}, "long") is None
    assert _parse_oanda_position_units({"long": {"units": "not-units"}}, "long") is None
    assert _parse_oanda_position_units({"long": {"units": "NaN"}}, "long") is None
    assert _parse_oanda_position_units({"long": {"units": "NONE"}}, "long") == 0
