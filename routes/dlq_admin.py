from typing import List, Optional

import json
import logging
import hmac
import hashlib
from datetime import timedelta

from fastapi import APIRouter, HTTPException, status, Depends, Header, Request

from config.limiter import limiter
from config.settings import settings
from dlq import redis_client
from celery_app import celery_app

from models.dlq_db import DLQEntry, RequeueResponse
from schemas import DLQReplayRequest, DLQDryRunResponse
from engine import get_calendar_engine
from utilities import adaptive_throttling
from utilities.security import (
    append_audit_event,
    verify_tenant_context,
    validate_tenant_access,
)

logger = logging.getLogger(__name__)


def require_admin(
    x_admin_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Simple admin auth dependency.

    Accepts either an `X-Admin-Key` header or `Authorization: Bearer <key>`.
    The header value must match `settings.security.app_master_key`.
    """

    expected = settings.security.app_master_key

    # Check X-Admin-Key first
    if x_admin_key and x_admin_key == expected:
        return True

    # Check Authorization header (Bearer)
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1]
        if token == expected:
            return True

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin auth required"
    )


router = APIRouter(
    prefix="/admin/dlq", tags=["Admin"], dependencies=[Depends(require_admin)]
)


def _require_dlq_tenant_scope(
    request: Request,
    *,
    operation: str,
    tenant_owner: int | str | None = None,
    entity_key: str | None = None,
):
    claims = validate_tenant_access(
        request,
        "dlq:replay",
        tenant_owner_id=tenant_owner,
        resource_key=entity_key,
    )
    if operation not in (claims.get("bounds", {}).get("allowed_operations") or []):
        raise HTTPException(
            status_code=403, detail="Operation not allowed for tenant scope"
        )
    return claims


@router.post("/replay", response_model=RequeueResponse)
@limiter.limit("10/minute")
def replay_dlq_message(
    request: Request,
    body: DLQReplayRequest,
    idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
):
    """Replay a DLQ message safely using Redis locks and idempotency.

    - Requires an `X-Idempotency-Key` header to prevent duplicate replays.
    - Moves messages with too many replays into a quarantine set.
    """

    claims = _require_dlq_tenant_scope(request, operation="replay")

    dlq_key = settings.redis.dlq_key
    lock_key = f"dlq:lock:{body.message_id}"
    idempotency_redis_key = f"idempotency:dlq:{idempotency_key}"

    # Idempotency check
    if redis_client.get(idempotency_redis_key):
        raise HTTPException(status_code=409, detail="Replay active or completed.")

    # Acquire lock
    if not redis_client.set(lock_key, "processing", nx=True, ex=60):
        raise HTTPException(status_code=423, detail="Message currently locked.")

    try:
        raw_list = redis_client.lrange(dlq_key, 0, -1)
        target_raw = None
        target_payload = None
        for raw in raw_list:
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            if payload.get("task_id") == body.message_id:
                target_raw = raw
                target_payload = payload
                break

        if not target_raw:
            raise HTTPException(status_code=404, detail="DLQ message not found")

        tenant_owner = target_payload.get("user_id")
        entity_key = f"user:{tenant_owner}"
        claims = _require_dlq_tenant_scope(
            request,
            operation="replay",
            tenant_owner=tenant_owner,
            entity_key=entity_key,
        )
        if str(claims.get("tenant_id")) != str(tenant_owner):
            raise HTTPException(
                status_code=403, detail="Tenant mismatch for requested DLQ record"
            )

        replay_count = int(target_payload.get("replay_count", 0))

        # Quarantine threshold
        if replay_count >= 3:
            # Add to quarantine set and remove from list
            try:
                redis_client.sadd("dlq:quarantine", json.dumps(target_payload))
                redis_client.lrem(dlq_key, 1, target_raw)
                try:
                    adaptive_throttling.adjust_based_on_metrics(
                        provider="llm", p95_latency_ms=1000.0, error_rate=0.5
                    )
                except Exception:
                    logger.exception("Adaptive throttling call failed")
            except Exception:
                logger.exception("Failed to move DLQ message to quarantine")

            append_audit_event(
                "dlq_replay_quarantined",
                actor_id=claims.get("user_id"),
                tenant_id=claims.get("tenant_id"),
                action="dlq:replay",
                resource=body.message_id,
                metadata={"tenant_owner": tenant_owner, "entity_key": entity_key},
                request_id=idempotency_key,
            )
            raise HTTPException(status_code=400, detail="Moved to quarantine.")

        target_payload["replay_count"] = replay_count + 1
        updated_raw = json.dumps(target_payload)
        redis_client.lrem(dlq_key, 1, target_raw)
        redis_client.rpush(dlq_key, updated_raw)

        redis_client.set(idempotency_redis_key, body.message_id, ex=3600)

        user_id = target_payload.get("user_id")
        request_text = target_payload.get("request_text")

        append_audit_event(
            "dlq_replay",
            actor_id=claims.get("user_id"),
            tenant_id=claims.get("tenant_id"),
            action="dlq:replay",
            resource=body.message_id,
            metadata={"tenant_owner": user_id, "request_text": request_text[:200]},
            request_id=idempotency_key,
        )

        try:
            async_result = celery_app.send_task(
                "tasks.execute_calendar_schedule_task",
                args=[user_id, request_text],
            )
            new_id = getattr(async_result, "id", None)
            return RequeueResponse(
                original_task_id=body.message_id, new_task_id=new_id, status="requeued"
            )
        except Exception as exc:
            logger.exception("Failed to requeue DLQ entry %s: %s", body.message_id, exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to requeue task",
            )

    finally:
        redis_client.delete(lock_key)


@router.post("/dry-run", response_model=DLQDryRunResponse)
@limiter.limit("30/minute")
def dlq_dry_run(
    request: Request,
    body: DLQReplayRequest,
):
    """Perform a dry-run (read-only) replay of a DLQ message.

    Returns a parsed function-call representation and an HMAC audit signature
    for the raw DLQ payload. This endpoint must NOT call external stateful
    systems (Google Calendar) — it only performs extraction and returns a
    signature for auditing.
    """

    claims = _require_dlq_tenant_scope(request, operation="replay")
    dlq_key = settings.redis.dlq_key

    raw_list = redis_client.lrange(dlq_key, 0, -1)
    target_raw = None
    target_payload = None
    for raw in raw_list:
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if payload.get("task_id") == body.message_id:
            target_raw = raw
            target_payload = payload
            break

    if not target_raw:
        raise HTTPException(status_code=404, detail="DLQ message not found")

    tenant_owner = target_payload.get("user_id")
    entity_key = f"user:{tenant_owner}"
    claims = _require_dlq_tenant_scope(
        request, operation="replay", tenant_owner=tenant_owner, entity_key=entity_key
    )
    if str(claims.get("tenant_id")) != str(tenant_owner):
        raise HTTPException(
            status_code=403, detail="Tenant mismatch for requested DLQ record"
        )

    request_text = target_payload.get("request_text")

    # Extract parsed function via the engine (no side-effects)
    try:
        engine_inst = get_calendar_engine()
        func_call = engine_inst.extract_calendar_function(request_text)
        parsed = func_call.model_dump()
    except Exception as exc:
        logger.exception("Dry-run extraction failed for %s: %s", body.message_id, exc)
        raise HTTPException(status_code=500, detail="Extraction failed")

    # HMAC audit signature over the raw payload using app master key
    try:
        master_hex = settings.security.app_master_key
        key = bytes.fromhex(master_hex)
        sig = hmac.new(key, target_raw.encode("utf-8"), hashlib.sha256).hexdigest()
    except Exception:
        logger.exception("Failed to compute HMAC for DLQ dry-run")
        sig = None

    append_audit_event(
        "dlq_dry_run",
        actor_id=claims.get("user_id"),
        tenant_id=claims.get("tenant_id"),
        action="dlq:dry_run",
        resource=body.message_id,
        metadata={"tenant_owner": tenant_owner, "entity_key": entity_key},
        request_id=body.message_id,
    )

    return DLQDryRunResponse(
        message_id=body.message_id,
        cached=False,
        parsed_function=parsed,
        hmac_signature=sig,
        note="Dry-run only; no downstream side-effects",
    )


@router.get("/", response_model=List[DLQEntry])
@limiter.limit("30/minute")
def list_dlq():
    """List entries in the Redis DLQ."""
    raw_list = redis_client.lrange(settings.redis.dlq_key, 0, -1)
    entries: List[DLQEntry] = []
    for raw in raw_list:
        try:
            payload = json.loads(raw)
            entries.append(DLQEntry(**payload))
        except Exception:
            logger.exception("Failed to parse DLQ entry: %s", raw)
    return entries


@router.post("/requeue", response_model=RequeueResponse)
@limiter.limit("20/minute")
def requeue_dlq_entry(task_id: str):
    """Requeue a single DLQ entry back to the Celery task queue.

    The client supplies the original `task_id` that was pushed into the DLQ.
    """
    raw_list = redis_client.lrange(settings.redis.dlq_key, 0, -1)

    for raw in raw_list:
        try:
            payload = json.loads(raw)
        except Exception:
            continue

        if payload.get("task_id") == task_id:
            user_id = payload.get("user_id")
            request_text = payload.get("request_text")

            try:
                async_result = celery_app.send_task(
                    "tasks.execute_calendar_schedule_task",
                    args=[user_id, request_text],
                )
                new_id = getattr(async_result, "id", None)

                redis_client.lrem(settings.redis.dlq_key, 1, raw)

                return RequeueResponse(
                    original_task_id=task_id, new_task_id=new_id, status="requeued"
                )

            except Exception as exc:
                logger.exception("Failed to requeue DLQ entry %s: %s", task_id, exc)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to requeue task",
                )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"DLQ entry with task_id {task_id} not found",
    )


@router.post("/requeue_all")
@limiter.limit("5/minute")
def requeue_all_dlq_entries():
    """Requeue all DLQ entries. Returns summary of requeued tasks."""
    raw_list = redis_client.lrange(settings.redis.dlq_key, 0, -1)
    summary = []

    for raw in raw_list:
        try:
            payload = json.loads(raw)
        except Exception:
            logger.exception("Skipping unparsable DLQ payload: %s", raw)
            continue

        task_id = payload.get("task_id")
        user_id = payload.get("user_id")
        request_text = payload.get("request_text")

        try:
            async_result = celery_app.send_task(
                "tasks.execute_calendar_schedule_task",
                args=[user_id, request_text],
            )
            new_id = getattr(async_result, "id", None)
            summary.append(
                {
                    "original_task_id": task_id,
                    "new_task_id": new_id,
                    "status": "requeued",
                }
            )
            redis_client.lrem(settings.redis.dlq_key, 1, raw)
        except Exception:
            logger.exception("Failed to requeue DLQ entry: %s", task_id)
            summary.append(
                {"original_task_id": task_id, "new_task_id": None, "status": "failed"}
            )

    return {"count": len(summary), "results": summary}


@router.delete("/{task_id}")
@limiter.limit("10/minute")
def delete_dlq_entry(task_id: str):
    """Delete a DLQ entry without requeueing."""
    raw_list = redis_client.lrange(settings.redis.dlq_key, 0, -1)

    for raw in raw_list:
        try:
            payload = json.loads(raw)
        except Exception:
            continue

        if payload.get("task_id") == task_id:
            redis_client.lrem(settings.redis.dlq_key, 1, raw)
            return {"deleted": task_id}

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"DLQ entry with task_id {task_id} not found",
    )
