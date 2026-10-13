import pytest
from unittest.mock import MagicMock, patch

pytest.importorskip("utilities.feature_flags")
from utilities.feature_flags import (
    get_feature_flag,
    set_feature_flag,
    get_rate_limit_capacity,
    set_rate_limit_capacity,
    get_token_refill_rate,
    set_token_refill_rate,
    get_circuit_breaker_threshold,
    set_circuit_breaker_threshold,
    is_background_job_throttled,
    set_background_job_throttle,
)


@pytest.mark.unit
def test_feature_flag_round_trip_via_redis_and_memory():
    with patch("utilities.feature_flags._redis") as redis_mock:
        redis_mock.get.return_value = None
        assert get_feature_flag("flag-a", default="fallback") == "fallback"

        payload = set_feature_flag("flag-a", True, ttl_seconds=60)
        assert payload["flag"] == "flag-a"


@pytest.mark.unit
def test_common_feature_flag_helpers_are_numeric_or_bool():
    with patch("utilities.feature_flags.get_feature_flag", return_value=42):
        assert get_rate_limit_capacity("x") == 42

    with patch("utilities.feature_flags.get_feature_flag", return_value=0.5):
        assert get_token_refill_rate("x") == pytest.approx(0.5)

    with patch("utilities.feature_flags.get_feature_flag", return_value=7):
        assert get_circuit_breaker_threshold("x") == 7

    with patch("utilities.feature_flags.get_feature_flag", return_value=True):
        assert is_background_job_throttled("job-x") is True


@pytest.mark.unit
def test_setters_return_payloads():
    with patch("utilities.feature_flags._redis") as redis_mock:
        set_rate_limit_capacity("x", 12)
        set_token_refill_rate("x", 0.75)
        set_circuit_breaker_threshold("x", 3)
        set_background_job_throttle("job-x", True)
        assert redis_mock.set.called
