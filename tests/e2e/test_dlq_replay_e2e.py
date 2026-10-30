"""End-to-End tests for DLQ replay, dry-run, and quarantine workflows."""

import json
import uuid
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from models.user_db import UserDB
from schemas import ScheduleCalendarEventFunction
from utilities.security import build_tenant_context


@pytest.mark.asyncio
async def test_dlq_auth_required(async_client: AsyncClient):
    """Test accessing DLQ admin endpoints without admin key returns 401."""
    response = await async_client.get("/admin/dlq/")
    assert response.status_code == 401
    assert "Admin auth required" in response.json().get("detail", "")

    replay_payload = {"message_id": "task-uuid-123"}
    response = await async_client.post(
        "/admin/dlq/replay", json=replay_payload
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_list_dlq_entries(
    async_client: AsyncClient, admin_headers: dict[str, str]
):
    """Test listing DLQ entries returns list of DLQEntry objects."""
    fake_dlq_payload = {
        "task_id": "task-dead-001",
        "user_id": 1,
        "request_text": "Schedule client pitch meeting",
        "error": "Google API 503 Backend Error",
        "created_at": "2026-10-30T12:00:00Z",
    }

    mock_sync_redis = MagicMock()
    mock_sync_redis.lrange.return_value = [json.dumps(fake_dlq_payload)]

    with patch("routes.dlq_admin.redis_client", mock_sync_redis):
        response = await async_client.get(
            "/admin/dlq/", headers=admin_headers
        )

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["task_id"] == "task-dead-001"
    assert data[0]["error"] == "Google API 503 Backend Error"


@pytest.mark.asyncio
async def test_dlq_dry_run_success(
    async_client: AsyncClient,
    admin_headers: dict[str, str],
    test_user: UserDB,
):
    """Test DLQ dry-run parses function call with HMAC audit signature."""
    msg_id = str(uuid.uuid4())
    fake_dlq_payload = {
        "task_id": msg_id,
        "user_id": test_user.id,
        "request_text": (
            "Schedule a quarterly review meeting tomorrow at 4 PM UTC"
        ),
        "error": "Timeout Error",
        "created_at": "2026-10-30T12:00:00Z",
    }

    tenant_jwt = build_tenant_context(
        tenant_id=test_user.id,
        scopes=["dlq:replay"],
        operation="replay",
        entity_key=f"user:{test_user.id}",
        bounds={"allowed_operations": ["replay"]},
    )

    headers = {
        **admin_headers,
        "X-Tenant-Context": tenant_jwt,
    }

    mock_sync_redis = MagicMock()
    mock_sync_redis.lrange.return_value = [json.dumps(fake_dlq_payload)]

    mock_func_call = ScheduleCalendarEventFunction(
        summary="Quarterly Review Meeting",
        description="",
        location="",
        start={"date_time": "2026-10-31T16:00:00Z", "time_zone": "UTC"},
        end={"date_time": "2026-10-31T17:00:00Z", "time_zone": "UTC"},
        attendees=[],
    )

    mock_engine = MagicMock()
    mock_engine.extract_calendar_function.return_value = mock_func_call

    with (
        patch("routes.dlq_admin.redis_client", mock_sync_redis),
        patch("routes.dlq_admin.get_calendar_engine", return_value=mock_engine),
        patch("routes.dlq_admin.append_audit_event"),
    ):
        response = await async_client.post(
            "/admin/dlq/dry-run",
            json={"message_id": msg_id},
            headers=headers,
        )

    assert response.status_code == 200
    data = response.json()
    assert data["message_id"] == msg_id
    assert data["parsed_function"]["summary"] == "Quarterly Review Meeting"
    assert data["hmac_signature"] is not None
    assert "Dry-run only" in data["note"]


@pytest.mark.asyncio
async def test_dlq_replay_success(
    async_client: AsyncClient,
    admin_headers: dict[str, str],
    test_user: UserDB,
):
    """Test successful DLQ replay requeues task with idempotency and audit."""
    msg_id = str(uuid.uuid4())
    new_task_id = str(uuid.uuid4())
    fake_dlq_payload = {
        "task_id": msg_id,
        "user_id": test_user.id,
        "request_text": "Schedule sync meeting with team",
        "replay_count": 0,
        "error": "Temporary Failure",
    }

    tenant_jwt = build_tenant_context(
        tenant_id=test_user.id,
        scopes=["dlq:replay"],
        operation="replay",
        entity_key=f"user:{test_user.id}",
        bounds={"allowed_operations": ["replay"]},
    )

    headers = {
        **admin_headers,
        "X-Tenant-Context": tenant_jwt,
        "X-Idempotency-Key": "idemp-key-e2e-001",
    }

    mock_sync_redis = MagicMock()
    mock_sync_redis.get.return_value = None
    mock_sync_redis.set.return_value = True
    mock_sync_redis.lrange.return_value = [json.dumps(fake_dlq_payload)]

    mock_async_result = MagicMock()
    mock_async_result.id = new_task_id

    with (
        patch("routes.dlq_admin.redis_client", mock_sync_redis),
        patch(
            "celery_app.celery_app.send_task", return_value=mock_async_result
        ),
        patch("routes.dlq_admin.append_audit_event"),
    ):
        response = await async_client.post(
            "/admin/dlq/replay",
            json={"message_id": msg_id},
            headers=headers,
        )

    assert response.status_code == 200
    data = response.json()
    assert data["original_task_id"] == msg_id
    assert data["new_task_id"] == new_task_id
    assert data["status"] == "requeued"


@pytest.mark.asyncio
async def test_dlq_replay_idempotency_conflict(
    async_client: AsyncClient,
    admin_headers: dict[str, str],
    test_user: UserDB,
):
    """Test DLQ replay with existing idempotency key returns 409 Conflict."""
    tenant_jwt = build_tenant_context(
        tenant_id=test_user.id,
        scopes=["dlq:replay"],
        operation="replay",
        entity_key=f"user:{test_user.id}",
        bounds={"allowed_operations": ["replay"]},
    )

    headers = {
        **admin_headers,
        "X-Tenant-Context": tenant_jwt,
        "X-Idempotency-Key": "idemp-key-duplicate",
    }

    mock_sync_redis = MagicMock()
    mock_sync_redis.get.return_value = b"msg-id-active"

    with patch("routes.dlq_admin.redis_client", mock_sync_redis):
        response = await async_client.post(
            "/admin/dlq/replay",
            json={"message_id": "msg-id-123"},
            headers=headers,
        )

    assert response.status_code == 409
    assert "Replay active or completed" in response.json().get("detail", "")


@pytest.mark.asyncio
async def test_dlq_replay_quarantine_threshold(
    async_client: AsyncClient,
    admin_headers: dict[str, str],
    test_user: UserDB,
):
    """Test DLQ replay with replay_count >= 3 moves message to quarantine."""
    msg_id = str(uuid.uuid4())
    quarantine_payload = {
        "task_id": msg_id,
        "user_id": test_user.id,
        "request_text": "Failing schedule request",
        "replay_count": 3,
        "error": "Persistent Error",
    }

    tenant_jwt = build_tenant_context(
        tenant_id=test_user.id,
        scopes=["dlq:replay"],
        operation="replay",
        entity_key=f"user:{test_user.id}",
        bounds={"allowed_operations": ["replay"]},
    )

    headers = {
        **admin_headers,
        "X-Tenant-Context": tenant_jwt,
        "X-Idempotency-Key": "idemp-key-quarantine-001",
    }

    mock_sync_redis = MagicMock()
    mock_sync_redis.get.return_value = None
    mock_sync_redis.set.return_value = True
    mock_sync_redis.lrange.return_value = [json.dumps(quarantine_payload)]

    with (
        patch("routes.dlq_admin.redis_client", mock_sync_redis),
        patch("routes.dlq_admin.append_audit_event"),
    ):
        response = await async_client.post(
            "/admin/dlq/replay",
            json={"message_id": msg_id},
            headers=headers,
        )

    assert response.status_code == 400
    assert "Moved to quarantine" in response.json().get("detail", "")
    mock_sync_redis.sadd.assert_called_once()


@pytest.mark.asyncio
async def test_dlq_replay_tenant_mismatch(
    async_client: AsyncClient,
    admin_headers: dict[str, str],
    test_user: UserDB,
):
    """Test DLQ replay returns 403 when record belongs to another tenant."""
    msg_id = str(uuid.uuid4())
    other_user_id = test_user.id + 9999
    mismatched_payload = {
        "task_id": msg_id,
        "user_id": other_user_id,
        "request_text": "Secret schedule request",
        "replay_count": 0,
    }

    tenant_jwt = build_tenant_context(
        tenant_id=test_user.id,
        scopes=["dlq:replay"],
        operation="replay",
        entity_key=f"user:{test_user.id}",
        bounds={"allowed_operations": ["replay"]},
    )

    headers = {
        **admin_headers,
        "X-Tenant-Context": tenant_jwt,
        "X-Idempotency-Key": "idemp-key-mismatch",
    }

    mock_sync_redis = MagicMock()
    mock_sync_redis.get.return_value = None
    mock_sync_redis.set.return_value = True
    mock_sync_redis.lrange.return_value = [json.dumps(mismatched_payload)]

    with patch("routes.dlq_admin.redis_client", mock_sync_redis):
        response = await async_client.post(
            "/admin/dlq/replay",
            json={"message_id": msg_id},
            headers=headers,
        )

    assert response.status_code == 403
    assert "Tenant mismatch" in response.json().get("detail", "")
