# routes/meeting_scheduler.py
# Now the worker knows exactly whose Google Calendar should be used.


from fastapi import APIRouter, Depends, HTTPException, status, Request
from datetime import datetime, timezone
import logging
import hashlib

from config.cache import get_redis_client
from config.settings import settings
from utilities.tokenizer import count_tokens
from utilities.redis_scripts import atomic_consume

from config.limiter import limiter
from auth import get_current_user
from models.user_db import UserDB
from schemas import (
    TaskStatusResponse,
    UserScheduleRequest,
)
from tasks.google_calender_meeting import execute_calendar_schedule_task
from telemetry.manager import get_shared_ledger
from messaging.bus import PayloadMismatchError
import os

router = APIRouter()


@router.post(
    "/api/v1/async-schedule",
    response_model=TaskStatusResponse,
)
@limiter.limit("5/minute;20/hour")  # Strict rate limit for LLM/Celery execution
async def async_schedule_meeting(
    request: Request,
    payload: UserScheduleRequest,
    user: UserDB = Depends(get_current_user),
):

    # Check circuit breaker health score from anomaly engine
    flag_anomaly_mgr = getattr(request.app.state, "flag_anomaly_manager", None)
    if flag_anomaly_mgr:
        try:
            if await flag_anomaly_mgr.is_circuit_open():
                health = await flag_anomaly_mgr.get_health_score()
                logging.getLogger(__name__).warning(
                    "Circuit breaker open for user %d; health=%.1f", user.id, health
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"System degraded (health {health:.1f}/100); please retry later",
                )
        except HTTPException:
            raise
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to check circuit breaker health"
            )

    # Dual-bucket enforcement via settings
    rl = settings.rate_limiting

    REQUEST_LIMIT = rl.requests_per_minute
    REQUEST_WINDOW = rl.request_window_seconds

    TOKEN_LIMIT = rl.tokens_per_hour
    TOKEN_WINDOW = rl.token_window_seconds

    # Token estimate using tokenizer
    token_estimate = max(
        1, count_tokens(payload.request_text, model="google/gemma-2-2b")
    )

    redis_client = await get_redis_client()

    now = int(datetime.now(timezone.utc).timestamp())
    req_min = now // REQUEST_WINDOW
    token_hour = now // TOKEN_WINDOW

    req_key = f"req_bucket:{user.id}:{req_min}"
    token_key = f"token_used:{user.id}:{token_hour}"

    # Atomic consume both buckets using Lua script
    success, new_req, new_token = await atomic_consume(
        redis_client,
        req_key,
        token_key,
        REQUEST_LIMIT,
        TOKEN_LIMIT,
        token_estimate,
        REQUEST_WINDOW,
        TOKEN_WINDOW,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded (requests or token quota)",
        )

    # Generate idempotency key from user_id and request_text to detect duplicate schedules
    idempotency_payload = {"user_id": user.id, "request_text": payload.request_text}
    idempotency_key = f"schedule_meeting:{user.id}:{hashlib.sha256(payload.request_text.encode()).hexdigest()[:16]}"

    # Use message bus for idempotent request processing with jittered retry
    message_bus = getattr(request.app.state, "message_bus", None)
    if not message_bus:
        logging.getLogger(__name__).warning(
            "Message bus not initialized; falling back to direct enqueue"
        )
        task = execute_calendar_schedule_task.delay(
            user_id=user.id,
            request_text=payload.request_text,
        )
        task_id = task.id
    else:
        # Define handler for bus to invoke with retry logic
        async def enqueue_task(msg_payload):
            task = execute_calendar_schedule_task.delay(
                user_id=msg_payload["user_id"],
                request_text=msg_payload["request_text"],
            )
            return {"task_id": task.id, "status": "PENDING"}

        try:
            result = await message_bus.process_with_retry(
                handler=enqueue_task,
                payload=idempotency_payload,
                idempotency_key=idempotency_key,
                max_retries=3,
            )
            task_id = result.get("task_id")
        except PayloadMismatchError as exc:
            # Payload tampering detected: key reuse with modified request
            logging.getLogger(__name__).warning(
                "Idempotency key reuse attack detected for user %d: %s",
                user.id,
                str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Request payload modified: idempotency key cannot be reused with different data",
            )
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "Failed to enqueue task via message bus: %s", exc
            )
            # Fallback to direct enqueue on bus failure
            task = execute_calendar_schedule_task.delay(
                user_id=user.id,
                request_text=payload.request_text,
            )
            task_id = task.id

    # Record enqueue event in shared audit ledger
    try:
        ledger = get_shared_ledger()
        ledger.append(
            "schedule_enqueued",
            {"task_id": task_id, "user_id": user.id},
            trace_id=task_id,
        )
    except Exception:
        logging.getLogger(__name__).exception(
            "Failed to write audit ledger enqueue event"
        )

    return TaskStatusResponse(
        task_id=task_id,
        status="PENDING",
    )
