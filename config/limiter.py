# config/limiter.py
import json
import logging

import redis
from slowapi import Limiter
from slowapi.util import get_remote_address
from fastapi import Request

from config.settings import settings
from utilities.security import append_audit_event, get_current_correlation_id
from utilities.feature_flags import get_rate_limit_capacity, get_token_refill_rate

logger = logging.getLogger(__name__)

_runtime_redis_client = None


def _get_runtime_redis_client():
    global _runtime_redis_client
    if _runtime_redis_client is None:
        _runtime_redis_client = redis.Redis.from_url(
            settings.redis.url,
            decode_responses=True,
        )
    return _runtime_redis_client


def get_runtime_limit(name: str, default_limit: str = "100/minute") -> str:
    redis_client = _get_runtime_redis_client()
    # First check Redis runtime overrides
    raw_value = redis_client.get(f"runtime:limit:{name}") if redis_client else None
    if raw_value:
        try:
            value = json.loads(raw_value)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                return value.get("limit", default_limit)
        except (TypeError, ValueError):
            pass
        return str(raw_value)

    # Fall back to feature flags
    try:
        capacity = get_rate_limit_capacity(name)
        return f"{capacity}/minute"
    except Exception:
        pass

    return default_limit


def set_runtime_limit(
    name: str, limit_value: str, ttl_seconds: int | None = None
) -> str:
    ttl_seconds = ttl_seconds or settings.rate_limit.hot_reload_ttl_seconds
    redis_client = _get_runtime_redis_client()
    if redis_client is not None:
        redis_client.set(
            f"runtime:limit:{name}",
            json.dumps(
                {"limit": str(limit_value), "updated_at": __import__("time").time()}
            ),
            ex=ttl_seconds,
        )
    append_audit_event(
        "rate_limit_policy_updated",
        actor_id="system",
        tenant_id="system",
        action="rate_limit.hot_reload",
        resource=name,
        metadata={"limit": str(limit_value), "ttl_seconds": ttl_seconds},
        correlation_id=get_current_correlation_id(),
    )
    return str(limit_value)


def get_identifier(request: Request) -> str:
    """
    Identifies the client by tenant ID, user ID, or IP address.
    """
    # 1. Prefer tenant ID (stored in request.state.tenant_context)
    if hasattr(request.state, "tenant_context") and request.state.tenant_context.get(
        "tenant_id"
    ):
        return f"tenant:{request.state.tenant_context['tenant_id']}"

    # 2. Prefer authenticated user ID (stored in session or request state)
    user_id = request.session.get("user_id") if hasattr(request, "session") else None
    if user_id:
        return f"user:{user_id}"

    # 3. Fall back to remote IP address
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=get_identifier,
    storage_uri=settings.redis.async_url,
    default_limits=[get_runtime_limit("default", "100/minute")],
)
