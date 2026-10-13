import pytest
from unittest.mock import MagicMock, patch

pytest.importorskip("utilities.adaptive_throttling")
from utilities.adaptive_throttling import (
    compute_refill_multiplier,
    set_provider_refill_multiplier,
    adjust_based_on_metrics,
    should_throttle_background_job,
    apply_dynamic_throttle_to_job,
    get_effective_refill_rate,
)


@pytest.mark.unit
def test_compute_refill_multiplier_returns_safe_range():
    assert 0.1 <= compute_refill_multiplier(50.0, 0.0) <= 1.0
    assert compute_refill_multiplier(10000.0, 0.9) == pytest.approx(0.1)


@pytest.mark.unit
def test_set_provider_refill_multiplier_persists_and_audits():
    fake_redis = MagicMock()
    fake_redis.set = MagicMock()
    fake_redis.expire = MagicMock()

    with patch("utilities.adaptive_throttling.redis_client", fake_redis), patch(
        "utilities.adaptive_throttling.append_audit_event"
    ) as audit_mock:
        set_provider_refill_multiplier("provider-a", 0.5, ttl_seconds=60)

        fake_redis.set.assert_called()
        fake_redis.expire.assert_called()
        audit_mock.assert_called_once()


@pytest.mark.unit
def test_adjust_based_on_metrics_returns_persisted_multiplier():
    with patch(
        "utilities.adaptive_throttling.set_provider_refill_multiplier"
    ) as set_mock:
        result = adjust_based_on_metrics("provider-b", 200.0, 0.2)
        set_mock.assert_called_once_with("provider-b", result)
        assert 0.1 <= result <= 1.0


@pytest.mark.unit
def test_background_job_throttle_helpers():
    with patch(
        "utilities.adaptive_throttling.is_background_job_throttled", return_value=True
    ):
        assert should_throttle_background_job("job-a") is True

    with patch(
        "utilities.adaptive_throttling.set_background_job_throttle",
        return_value={"ok": True},
    ) as set_mock:
        result = apply_dynamic_throttle_to_job("job-a", True, ttl_seconds=30)
        set_mock.assert_called_once_with("job-a", True, 30)
        assert result == {"ok": True}


@pytest.mark.unit
def test_get_effective_refill_rate_uses_min_of_feature_and_runtime(monkeypatch):
    monkeypatch.setattr(
        "utilities.adaptive_throttling.get_token_refill_rate", lambda provider: 0.6
    )
    fake_redis = MagicMock()
    fake_redis.get.return_value = b"0.4"

    with patch("utilities.adaptive_throttling.redis_client", fake_redis):
        assert get_effective_refill_rate("provider-c") == pytest.approx(0.4)
