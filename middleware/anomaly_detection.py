"""Middleware for real-time latency tracking and anomaly detection.

Measures request latency and triggers anomaly detection, which can
automatically adjust circuit breaker states and throttle background jobs
if performance degrades.
"""

import time
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from utilities.anomaly_detector import detect_anomaly
from utilities.circuit_breaker import trigger_half_open_if_anomaly
from utilities.security import set_current_correlation_id

logger = logging.getLogger(__name__)


class AnomalyDetectionMiddleware(BaseHTTPMiddleware):
    """Track request latency and detect performance anomalies."""

    async def dispatch(self, request: Request, call_next):
        start_time = time.perf_counter()

        # Get or set correlation ID for this request
        correlation_id = request.headers.get("X-Correlation-ID")
        if correlation_id:
            set_current_correlation_id(correlation_id)

        response = await call_next(request)

        # Measure latency
        latency_ms = (time.perf_counter() - start_time) * 1000

        # Track latency and check for anomalies
        operation = f"{request.method} {request.url.path}"
        try:
            result = detect_anomaly(operation, latency_ms)

            # If anomaly detected, potentially trigger half-open circuit for dependent services
            if result["is_anomaly"]:
                # Only trigger circuit for high-latency APIs (non-analytics, non-status endpoints)
                if (
                    "/admin/analytics" not in operation
                    and "/task-status" not in operation
                ):
                    trigger_half_open_if_anomaly(provider_name="llm", ttl_seconds=30)
                    logger.warning(
                        "Anomaly in %s (P95=%.2fms); triggered half-open for LLM provider",
                        operation,
                        result["p95"],
                    )
        except Exception:
            logger.exception("Failed to process anomaly detection for %s", operation)

        # Add latency header to response for client visibility
        response.headers["X-Response-Time-Ms"] = str(round(latency_ms, 2))

        return response
