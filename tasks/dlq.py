# dlq.py

import json
from datetime import datetime, timezone

import redis

from config.settings import settings
from utilities.security import build_tenant_context

# Synchronous Redis client for error handlers running outside asyncio.run()
redis_client = redis.Redis.from_url(settings.redis.url, decode_responses=True)


def push_dead_letter(
    task_id: str,
    user_id: int,
    request_text: str,
    error: str,
    tenant_context: str | None = None,
):
    payload = {
        "task_id": task_id,
        "user_id": user_id,
        "request_text": request_text,
        "error": error,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tenant_context": tenant_context
        or build_tenant_context(
            tenant_id=user_id,
            scopes=["calendar:read", "calendar:write"],
            operation="dlq:enqueue",
            entity_key=f"user:{user_id}",
            bounds={"user_id": user_id, "allowed_operations": ["schedule", "replay"]},
        ),
    }

    redis_client.rpush(
        settings.redis.dlq_key,
        json.dumps(payload),
    )
