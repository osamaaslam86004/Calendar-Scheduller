import hashlib
import inspect
import logging
import time

import redis.exceptions
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from config.cache import get_redis_client
from utilities.security import get_request_tenant_context

logger = logging.getLogger(__name__)


class TenantContextMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        global_rate_limit: int = 1000,
        cluster_nodes: int = 3,
        redis_client=None,
    ):
        super().__init__(app)
        self.global_rate_limit = global_rate_limit
        self.cluster_nodes = cluster_nodes
        self.redis_client = redis_client
        self.local_rate_limit = max(1, self.global_rate_limit // self.cluster_nodes)
        self.local_request_counts = {}
        self.local_window_start = time.time()
        self.window_duration = 60

    @staticmethod
    async def _await_if_needed(value):
        if inspect.isawaitable(value):
            return await value
        return value

    async def _get_redis_client(self):
        if self.redis_client is not None:
            return self.redis_client
        return await get_redis_client()

    async def _redis_ping(self, redis_client):
        return await self._await_if_needed(redis_client.ping())

    async def _redis_get(self, redis_client, key):
        return await self._await_if_needed(redis_client.get(key))

    async def _redis_incr(self, redis_client, key):
        return await self._await_if_needed(redis_client.incr(key))

    async def _redis_expire(self, redis_client, key, ttl):
        return await self._await_if_needed(redis_client.expire(key, ttl))

    def _reset_local_window_if_needed(self):
        now = time.time()
        if now - self.local_window_start > self.window_duration:
            self.local_request_counts = {}
            self.local_window_start = now

    def _check_local_rate_limit(self, tenant_id: str) -> bool:
        self._reset_local_window_if_needed()
        count = self.local_request_counts.get(tenant_id, 0)
        if count >= self.local_rate_limit:
            return False
        self.local_request_counts[tenant_id] = count + 1
        return True

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith(("/docs", "/openapi", "/redoc", "/auth")):
            return await call_next(request)

        claims = get_request_tenant_context(request)
        tenant_id = request.headers.get("X-Tenant-ID")
        auth_token = request.headers.get("Authorization")

        if claims:
            claim_tenant_id = claims.get("tenant_id") or claims.get("sub")
            if claim_tenant_id is not None:
                claim_tenant_id = str(claim_tenant_id)
                if tenant_id and str(tenant_id) != claim_tenant_id:
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": "Tenant ID mismatch between JWT and X-Tenant-ID header"
                        },
                    )
                tenant_id = claim_tenant_id

        if not tenant_id:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Tenant context required: include a signed JWT tenant context or X-Tenant-ID header."
                },
            )

        if not auth_token and not claims:
            return JSONResponse(
                status_code=401, content={"detail": "Missing Authorization header"}
            )

        tenant_key = f"tenant:{tenant_id}"
        redis_available = False
        redis_client = None

        try:
            redis_client = await self._get_redis_client()
            await self._redis_ping(redis_client)
            redis_available = True
        except (
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
            Exception,
        ) as exc:
            logger.warning(
                "Redis unavailable, falling back to local rate limits: %s", exc
            )
            redis_available = False

        if auth_token and redis_available and redis_client:
            token_hash = hashlib.sha256(auth_token.encode("utf-8")).hexdigest()
            is_revoked = await self._redis_get(
                redis_client, f"revoked_token:{token_hash}"
            )
            if is_revoked:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Token revoked (authorization cache hit)"},
                )

            rate_key = f"rate_limit:{tenant_key}"
            current_count = await self._redis_incr(redis_client, rate_key)
            if current_count == 1:
                await self._redis_expire(redis_client, rate_key, self.window_duration)

            if current_count > self.global_rate_limit:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Global Rate Limit Exceeded"},
                )
        else:
            if not self._check_local_rate_limit(tenant_id):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Local Rate Limit Exceeded (Fallback Mode)"},
                )

        request.state.tenant_context = {
            "tenant_id": tenant_id,
            "user_id": claims.get("user_id") if claims else None,
            "authenticated": bool(claims or auth_token),
            "fallback_mode": not redis_available,
            "tenant_key": tenant_key,
            "scopes": claims.get("scopes", []) if claims else [],
        }
        request.state.tenant_claims = claims or {}
        request.state.tenant_id = tenant_id
        request.state.user_id = request.state.tenant_context["user_id"]
        request.state.authenticated = request.state.tenant_context["authenticated"]

        response = await call_next(request)
        return response
