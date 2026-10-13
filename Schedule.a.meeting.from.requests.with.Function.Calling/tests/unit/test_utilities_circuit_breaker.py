import pytest
from unittest.mock import patch

pytest.importorskip("utilities.circuit_breaker")
from utilities.circuit_breaker import (
    is_tripped,
    trip,
    clear,
    set_breaker_policy,
    get_breaker_policy,
    trigger_half_open_if_anomaly,
)


@pytest.mark.unit
def test_trip_and_clear_provider_state():
    with patch("utilities.circuit_breaker._redis") as redis_mock:
        redis_mock.get.return_value = None
        assert is_tripped("provider-x") is False

        trip("provider-x", ttl_seconds=30)
        redis_mock.set.assert_called_once()

        clear("provider-x")
        redis_mock.delete.assert_called_once()


@pytest.mark.unit
def test_set_and_get_breaker_policy():
    with patch("utilities.circuit_breaker._redis") as redis_mock:
        redis_mock.get.return_value = (
            '{"provider": "p", "open_threshold": 3, "reset_timeout_seconds": 30}'
        )
        policy = set_breaker_policy(
            "p", open_threshold=3, reset_timeout_seconds=30, ttl_seconds=90
        )
        assert policy["provider"] == "p"
        assert policy["open_threshold"] == 3
        assert get_breaker_policy("p")["provider"] == "p"


@pytest.mark.unit
def test_trigger_half_open_if_anomaly_checks_threshold():
    with patch(
        "utilities.circuit_breaker.should_trigger_half_open", return_value=True
    ), patch("utilities.circuit_breaker.trip") as trip_mock:
        assert trigger_half_open_if_anomaly("p", ttl_seconds=15) is True
        trip_mock.assert_called_once_with("p", ttl_seconds=15, via_anomaly=True)

    with patch(
        "utilities.circuit_breaker.should_trigger_half_open", return_value=False
    ):
        assert trigger_half_open_if_anomaly("p") is False
