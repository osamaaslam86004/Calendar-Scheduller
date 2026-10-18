"""Admin endpoints for dynamic feature flag management and anomaly response tuning."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from config.limiter import limiter
from utilities.feature_flags import (
    get_feature_flag,
    set_feature_flag,
    get_rate_limit_capacity,
    set_rate_limit_capacity,
    get_token_refill_rate,
    set_token_refill_rate,
    get_circuit_breaker_threshold,
    set_circuit_breaker_threshold,
    get_oauth_grace_period_hours,
    set_oauth_grace_period_hours,
    is_background_job_throttled,
    set_background_job_throttle,
    is_feature_enabled,
    set_feature_enabled,
)
from utilities.adaptive_throttling import (
    apply_dynamic_throttle_to_job,
    get_effective_refill_rate,
)
from utilities.anomaly_detector import LatencyPercentileTracker
from utilities.security import validate_tenant_access

router = APIRouter(prefix="/api/v1/admin/flags", tags=["admin", "feature-flags"])


class FeatureFlagUpdate(BaseModel):
    """Request to update a feature flag."""

    flag_name: str = Field(..., description="Feature flag name")
    value: str | bool | int | float = Field(..., description="New flag value")
    ttl_seconds: int | None = Field(None, description="TTL in seconds (default: 300)")


class RateLimitUpdate(BaseModel):
    """Request to update rate limit capacity."""

    provider: str = Field(default="default", description="Provider name")
    capacity: int = Field(..., ge=1, description="New capacity (requests/minute)")
    ttl_seconds: int | None = Field(None, description="TTL in seconds")


class CircuitBreakerUpdate(BaseModel):
    """Request to update circuit breaker threshold."""

    provider: str = Field(default="default", description="Provider name")
    threshold: int = Field(..., ge=1, description="Failure threshold before opening")
    ttl_seconds: int | None = Field(None, description="TTL in seconds")


class TokenRefillUpdate(BaseModel):
    """Request to update token refill rate."""

    provider: str = Field(default="default", description="Provider name")
    rate: float = Field(..., ge=0.1, le=2.0, description="Refill rate multiplier")
    ttl_seconds: int | None = Field(None, description="TTL in seconds")


class BackgroundJobThrottleUpdate(BaseModel):
    """Request to throttle background jobs."""

    job_type: str = Field(
        ..., description="Background job type (e.g., 'calendar_sync')"
    )
    throttled: bool = Field(..., description="Whether to throttle the job type")
    ttl_seconds: int | None = Field(None, description="TTL in seconds")


@router.post("/set")
@limiter.limit("20/minute")
async def set_flag(request: Request, update: FeatureFlagUpdate):
    """Set a feature flag dynamically."""
    # Validate admin access
    validate_tenant_access(request, "admin", tenant_owner_id=None)

    result = set_feature_flag(update.flag_name, update.value, update.ttl_seconds)
    return {
        "status": "ok" if "error" not in result else "error",
        "flag": update.flag_name,
        "value": update.value,
        **result,
    }


@router.get("/get/{flag_name}")
@limiter.limit("100/minute")
async def get_flag(request: Request, flag_name: str):
    """Get current value of a feature flag."""
    # Validate admin access
    validate_tenant_access(request, "admin", tenant_owner_id=None)

    value = get_feature_flag(flag_name)
    if value is None:
        return {"flag": flag_name, "value": None, "status": "not_found"}
    return {"flag": flag_name, "value": value, "status": "ok"}


@router.post("/rate-limit/set")
@limiter.limit("20/minute")
async def update_rate_limit_capacity(request: Request, update: RateLimitUpdate):
    """Update rate limit capacity for a provider."""
    validate_tenant_access(request, "admin", tenant_owner_id=None)

    result = set_rate_limit_capacity(
        update.provider, update.capacity, update.ttl_seconds
    )
    return {
        "status": "ok",
        "provider": update.provider,
        "capacity": update.capacity,
        "previous_capacity": get_rate_limit_capacity(update.provider),
        **result,
    }


@router.post("/circuit-breaker/set")
@limiter.limit("20/minute")
async def update_circuit_breaker(request: Request, update: CircuitBreakerUpdate):
    """Update circuit breaker threshold for a provider."""
    validate_tenant_access(request, "admin", tenant_owner_id=None)

    result = set_circuit_breaker_threshold(
        update.provider, update.threshold, update.ttl_seconds
    )
    return {
        "status": "ok",
        "provider": update.provider,
        "threshold": update.threshold,
        "previous_threshold": get_circuit_breaker_threshold(update.provider),
        **result,
    }


@router.post("/token-refill/set")
@limiter.limit("20/minute")
async def update_token_refill_rate(request: Request, update: TokenRefillUpdate):
    """Update token refill rate multiplier for a provider."""
    validate_tenant_access(request, "admin", tenant_owner_id=None)

    result = set_token_refill_rate(update.provider, update.rate, update.ttl_seconds)
    return {
        "status": "ok",
        "provider": update.provider,
        "refill_rate": update.rate,
        "effective_rate": get_effective_refill_rate(update.provider),
        **result,
    }


@router.post("/background-job/throttle")
@limiter.limit("20/minute")
async def throttle_background_job(
    request: Request, update: BackgroundJobThrottleUpdate
):
    """Enable/disable throttling for a background job type."""
    validate_tenant_access(request, "admin", tenant_owner_id=None)

    result = apply_dynamic_throttle_to_job(
        update.job_type, update.throttled, update.ttl_seconds
    )
    return {
        "status": "ok",
        "job_type": update.job_type,
        "throttled": update.throttled,
        **result,
    }


@router.get("/latency-percentiles/{operation}")
@limiter.limit("100/minute")
async def get_latency_percentiles(request: Request, operation: str):
    """Get current latency percentiles for an operation."""
    validate_tenant_access(request, "admin", tenant_owner_id=None)

    percentiles = LatencyPercentileTracker.from_redis(operation)
    if not percentiles:
        return {"operation": operation, "status": "no_data"}
    return {"operation": operation, "status": "ok", **percentiles}


@router.post("/feature/toggle")
@limiter.limit("20/minute")
async def toggle_feature(request: Request, feature_name: str, enabled: bool):
    """Enable/disable a feature flag."""
    validate_tenant_access(request, "admin", tenant_owner_id=None)

    result = set_feature_enabled(feature_name, enabled)
    return {
        "status": "ok",
        "feature": feature_name,
        "enabled": enabled,
        **result,
    }
