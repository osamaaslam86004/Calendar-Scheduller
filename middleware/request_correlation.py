import logging
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from utilities.security import set_current_correlation_id

logger = logging.getLogger(__name__)


class RequestCorrelationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        correlation_id = request.headers.get("X-Correlation-ID") or request.headers.get(
            "X-Request-ID"
        )
        if not correlation_id:
            correlation_id = uuid.uuid4().hex

        request.state.correlation_id = correlation_id
        request.state.request_id = correlation_id
        set_current_correlation_id(correlation_id)

        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        return response
