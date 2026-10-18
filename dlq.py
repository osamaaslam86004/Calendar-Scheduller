# dlq.py

import json
import os
import redis
from datetime import datetime, timezone
from config.settings import settings

# Initialize Sync Redis Client using centralized Redis settings
redis_client = redis.Redis.from_url(settings.redis.url, decode_responses=True)


def push_dead_letter(
    task_id: str,
    user_id: int,
    request_text: str,
    error: str,
):

    payload = {
        "task_id": task_id,
        "user_id": user_id,
        "request_text": request_text,
        "error": error,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    redis_client.rpush(
        settings.redis.dlq_key,
        json.dumps(payload),
    )
