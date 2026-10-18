"""Dynamic feature flagging and configuration management via Redis Pub/Sub.

Enables real-time adjustment of rate limits, circuit breaker thresholds, OAuth
grace periods, and feature flags without requiring service restarts.
"""

import json
import logging
import threading
from typing import Any, Callable, Dict, Optional

import redis

from config.settings import settings
from utilities.security import append_audit_event, get_current_correlation_id

logger = logging.getLogger(__name__)
_redis = redis.Redis.from_url(settings.redis.url, decode_responses=True)
_pubsub = None
_feature_flags = {}
_feature_lock = threading.RLock()


class FeatureFlagSubscriber:
    """Subscribe to feature flag changes via Redis Pub/Sub."""

    def __init__(self):
        self.pubsub = _redis.pubsub()
        self.flags = {}
        self.callbacks = {}
        self.thread = None
        self.running = False

    def subscribe(self, *channels: str):
        """Subscribe to feature flag channels."""
        self.pubsub.subscribe(*channels)
        logger.info("Subscribed to feature flag channels: %s", channels)

    def on_flag_change(self, flag_name: str, callback: Callable[[str, Any], None]):
        """Register a callback for flag changes."""
        self.callbacks[flag_name] = callback

    def start(self):
        """Start listening for flag updates in background thread."""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.thread.start()
        logger.info("Feature flag subscriber started")

    def stop(self):
        """Stop listening for flag updates."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        self.pubsub.close()
        logger.info("Feature flag subscriber stopped")

    def _listen_loop(self):
        """Listen for messages and trigger callbacks."""
        try:
            for message in self.pubsub.listen():
                if not self.running:
                    break
                if message["type"] == "message":
                    try:
                        data = json.loads(message["data"])
                        flag_name = data.get("flag")
                        flag_value = data.get("value")
                        self.flags[flag_name] = flag_value
                        if flag_name in self.callbacks:
                            self.callbacks[flag_name](flag_name, flag_value)
                        logger.info(
                            "Updated feature flag %s = %s", flag_name, flag_value
                        )
                    except (json.JSONDecodeError, KeyError):
                        logger.exception("Invalid flag update message: %s", message)
        except Exception:
            logger.exception("Error in feature flag listener loop")

    def get(self, flag_name: str, default: Any = None) -> Any:
        """Get current value of a feature flag."""
        with _feature_lock:
            return self.flags.get(flag_name, default)


def initialize_feature_flags():
    """Initialize global feature flag subscriber."""
    global _pubsub
    if _pubsub is None:
        _pubsub = FeatureFlagSubscriber()
        _pubsub.subscribe("feature:flags:*")
        _pubsub.start()
        _load_initial_flags()
        logger.info("Feature flag system initialized")


def _load_initial_flags():
    """Load all feature flags from Redis on startup."""
    try:
        pattern = "feature:flag:*"
        for key in _redis.scan_iter(match=pattern):
            flag_name = key.replace("feature:flag:", "").replace(":", "")
            raw_value = _redis.get(key)
            if raw_value:
                try:
                    value = json.loads(raw_value)
                    _feature_flags[flag_name] = value
                except (json.JSONDecodeError, TypeError):
                    _feature_flags[flag_name] = raw_value
    except Exception:
        logger.exception("Failed to load initial feature flags")


def get_feature_flag(flag_name: str, default: Any = None) -> Any:
    """Get current value of a feature flag with fallback."""
    # First check in-memory cache
    with _feature_lock:
        if flag_name in _feature_flags:
            return _feature_flags[flag_name]

    # Then check Redis
    try:
        raw_value = _redis.get(f"feature:flag:{flag_name}")
        if raw_value:
            try:
                value = json.loads(raw_value)
            except (json.JSONDecodeError, TypeError):
                value = raw_value
            with _feature_lock:
                _feature_flags[flag_name] = value
            return value
    except Exception:
        logger.exception("Failed to get feature flag %s", flag_name)

    return default


def set_feature_flag(
    flag_name: str, value: Any, ttl_seconds: int | None = None
) -> dict:
    """Set a feature flag and publish to subscribers."""
    ttl_seconds = ttl_seconds or settings.rate_limit.hot_reload_ttl_seconds
    payload = {
        "flag": flag_name,
        "value": value,
        "updated_at": __import__("time").time(),
    }

    try:
        # Store in Redis
        _redis.set(
            f"feature:flag:{flag_name}",
            json.dumps(payload, default=str),
            ex=ttl_seconds,
        )

        # Update in-memory cache
        with _feature_lock:
            _feature_flags[flag_name] = value

        # Publish to subscribers
        _redis.publish(f"feature:flags:{flag_name}", json.dumps(payload, default=str))

        append_audit_event(
            "feature_flag_updated",
            actor_id="system",
            tenant_id="system",
            action="feature.flag_update",
            resource=flag_name,
            metadata={"value": str(value), "ttl_seconds": ttl_seconds},
            correlation_id=get_current_correlation_id(),
        )

        logger.info("Set feature flag %s = %s", flag_name, value)
        return payload
    except Exception:
        logger.exception("Failed to set feature flag %s", flag_name)
        return {"flag": flag_name, "error": "flag_write_failed"}


# Common Feature Flags
def get_rate_limit_capacity(provider: str = "default") -> int:
    """Get current rate limit capacity for provider."""
    return get_feature_flag(f"rate_limit:capacity:{provider}", default=100)


def set_rate_limit_capacity(
    provider: str, capacity: int, ttl_seconds: int | None = None
):
    """Set rate limit capacity for provider."""
    return set_feature_flag(f"rate_limit:capacity:{provider}", capacity, ttl_seconds)


def get_token_refill_rate(provider: str = "default") -> float:
    """Get current token refill rate multiplier."""
    return get_feature_flag(f"token:refill_rate:{provider}", default=1.0)


def set_token_refill_rate(provider: str, rate: float, ttl_seconds: int | None = None):
    """Set token refill rate multiplier."""
    return set_feature_flag(f"token:refill_rate:{provider}", rate, ttl_seconds)


def get_circuit_breaker_threshold(provider: str = "default") -> int:
    """Get current circuit breaker failure threshold."""
    return get_feature_flag(f"circuit_breaker:threshold:{provider}", default=5)


def set_circuit_breaker_threshold(
    provider: str, threshold: int, ttl_seconds: int | None = None
):
    """Set circuit breaker failure threshold."""
    return set_feature_flag(
        f"circuit_breaker:threshold:{provider}", threshold, ttl_seconds
    )


def get_oauth_grace_period_hours(provider: str = "default") -> int:
    """Get current OAuth token grace period in hours."""
    return get_feature_flag(f"oauth:grace_period_hours:{provider}", default=24)


def set_oauth_grace_period_hours(
    provider: str, hours: int, ttl_seconds: int | None = None
):
    """Set OAuth token grace period in hours."""
    return set_feature_flag(f"oauth:grace_period_hours:{provider}", hours, ttl_seconds)


def is_background_job_throttled(job_type: str) -> bool:
    """Check if a background job type is currently throttled."""
    return get_feature_flag(f"throttle:background_job:{job_type}", default=False)


def set_background_job_throttle(
    job_type: str, throttled: bool, ttl_seconds: int | None = None
):
    """Enable/disable throttling for a background job type."""
    return set_feature_flag(
        f"throttle:background_job:{job_type}", throttled, ttl_seconds
    )


def is_feature_enabled(feature_name: str) -> bool:
    """Check if a feature is enabled."""
    return get_feature_flag(f"feature:enabled:{feature_name}", default=False)


def set_feature_enabled(
    feature_name: str, enabled: bool, ttl_seconds: int | None = None
):
    """Enable/disable a feature."""
    return set_feature_flag(f"feature:enabled:{feature_name}", enabled, ttl_seconds)
