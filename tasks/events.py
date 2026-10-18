# events.py (Redis event publisher)

import json
import redis

from config.settings import settings
from utilities.security import append_audit_event, get_current_correlation_id

redis_client = redis.Redis.from_url(settings.redis.url, decode_responses=True)


def publish_task_event(
    task_id: str,
    event: dict,
) -> None:
    channel = f"task_events:{task_id}"
    correlation_id = event.get("correlation_id") or get_current_correlation_id()
    if correlation_id:
        event["correlation_id"] = correlation_id
    redis_client.publish(channel, json.dumps(event))
    append_audit_event(
        event_type=event.get("type", "task_event"),
        actor_id=event.get("user_id"),
        tenant_id=event.get("tenant_id"),
        action="task_event",
        resource=task_id,
        metadata={"task_event": event},
        request_id=task_id,
        correlation_id=correlation_id,
    )
