"""Unit tests for the fail-closed exchange clock guard."""
import pytest

from app.execution import RiskBlocked, _verify_exchange_clock


class FakeClockBroker:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    def server_time(self):
        if self.error:
            raise self.error
        return self.value


def test_clock_guard_accepts_verified_time_within_one_second():
    assert _verify_exchange_clock(FakeClockBroker(1_700_000_000_500), local_time_ms=1_700_000_000_000) == 500


@pytest.mark.parametrize("value", [None, 0, -1, "not-a-time", float("inf"), True])
def test_clock_guard_rejects_missing_or_invalid_time(value):
    with pytest.raises(RiskBlocked):
        _verify_exchange_clock(FakeClockBroker(value), local_time_ms=1_700_000_000_000)


def test_clock_guard_rejects_drift_greater_than_one_second():
    with pytest.raises(RiskBlocked, match="drift exceeds safety limit"):
        _verify_exchange_clock(FakeClockBroker(1_700_000_002_000), local_time_ms=1_700_000_000_000)


def test_clock_guard_rejects_provider_network_error():
    with pytest.raises(RiskBlocked, match="clock verification failed"):
        _verify_exchange_clock(FakeClockBroker(error=TimeoutError("exchange clock timeout")), local_time_ms=1_700_000_000_000)
