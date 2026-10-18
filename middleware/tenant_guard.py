import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from utilities.security import get_request_tenant_context

logger = logging.getLogger(__name__)


class TenantGuardMiddleware(BaseHTTPMiddleware):
    """Backward-compatible alias for the merged tenant validation + context middleware."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (
            path.startswith("/docs")
            or path.startswith("/openapi")
            or path.startswith("/redoc")
        ):
            return await call_next(request)

        claims = get_request_tenant_context(request)
        if claims is None:
            if path.startswith("/auth"):
                return await call_next(request)
            return JSONResponse(
                {
                    "detail": "Tenant context required: include a signed JWT tenant context or X-Tenant-Context header."
                },
                status_code=401,
            )

        request.state.tenant_claims = claims
        request.state.tenant_id = claims.get("tenant_id")
        request.state.user_id = claims.get("user_id")

        response = await call_next(request)
        return response
