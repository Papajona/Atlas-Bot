import pytest

from scripts.evaluate_original_oanda_oos import granularity_bars_per_year


@pytest.mark.parametrize(
    ("granularity", "expected"),
    [
        ("D", 252),
        ("H1", 6048),
        ("M5", 72576),
        ("W", 52),
        ("M", 12),
    ],
)
def test_oanda_granularity_maps_to_appropriate_annualization(granularity, expected):
    assert granularity_bars_per_year(granularity) == expected


def test_unknown_granularity_fails_closed():
    with pytest.raises(ValueError, match="Unsupported OANDA granularity"):
        granularity_bars_per_year("UNKNOWN")
