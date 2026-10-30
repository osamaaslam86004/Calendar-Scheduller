"""End-to-End tests for meeting scheduler and task polling workflows."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from messaging.bus import PayloadMismatchError


@pytest.mark.asyncio
async def test_async_schedule_success(authenticated_client: AsyncClient):
    """Test successful async schedule request enqueues task and returns PENDING."""
    payload = {
        "request_text": (
            "Schedule a strategy sync with alice@example.com and "
            "bob@example.com tomorrow at 3 PM UTC for 45 minutes."
        )
    }
    fake_task_id = str(uuid.uuid4())

    mock_celery_task = MagicMock()
    mock_celery_task.id = fake_task_id

    mock_redis = AsyncMock()

    with (
        patch("config.cache.get_redis_client", return_value=mock_redis),
        patch(
            "utilities.redis_scripts.atomic_consume",
            return_value=(True, 1, 100),
        ),
        patch(
            "tasks.google_calender_meeting.execute_calendar_schedule_task.delay",
            return_value=mock_celery_task,
        ),
    ):
        response = await authenticated_client.post(
            "/api/v1/async-schedule", json=payload
        )

    assert response.status_code == 200
    data = response.json()
    assert "task_id" in data
    assert data["status"] == "PENDING"


@pytest.mark.asyncio
async def test_async_schedule_rate_limiting(
    authenticated_client: AsyncClient,
):
    """Test rate limit enforcement returns 429 when quota exceeded."""
    payload = {
        "request_text": (
            "Schedule a quick standup with dev-team@company.com "
            "tomorrow at 9 AM UTC."
        )
    }
    mock_redis = AsyncMock()

    with (
        patch("config.cache.get_redis_client", return_value=mock_redis),
        patch(
            "utilities.redis_scripts.atomic_consume",
            return_value=(False, 6, 50001),
        ),
    ):
        response = await authenticated_client.post(
            "/api/v1/async-schedule", json=payload
        )

    assert response.status_code == 429
    assert "Rate limit exceeded" in response.json().get("detail", "")


@pytest.mark.asyncio
async def test_async_schedule_circuit_breaker_open(
    authenticated_client: AsyncClient,
):
    """Test circuit breaker open state raises 503 when degraded."""
    payload = {
        "request_text": (
            "Schedule a project kickoff meeting tomorrow at 2 PM UTC."
        )
    }

    mock_anomaly_mgr = AsyncMock()
    mock_anomaly_mgr.is_circuit_open.return_value = True
    mock_anomaly_mgr.get_health_score.return_value = 45.0

    from main import app

    app.state.flag_anomaly_manager = mock_anomaly_mgr

    response = await authenticated_client.post(
        "/api/v1/async-schedule", json=payload
    )

    assert response.status_code == 503
    assert "System degraded" in response.json().get("detail", "")


@pytest.mark.asyncio
async def test_async_schedule_idempotency_payload_mismatch(
    authenticated_client: AsyncClient,
):
    """Test idempotency key reuse with modified payload raises 422."""
    payload = {
        "request_text": (
            "Schedule a budget review meeting with finance@company.com."
        )
    }

    mock_redis = AsyncMock()
    mock_bus = AsyncMock()
    mock_bus.process_with_retry.side_effect = PayloadMismatchError(
        "Payload mismatch for key"
    )

    from main import app

    app.state.message_bus = mock_bus

    with (
        patch("config.cache.get_redis_client", return_value=mock_redis),
        patch(
            "utilities.redis_scripts.atomic_consume",
            return_value=(True, 1, 50),
        ),
    ):
        response = await authenticated_client.post(
            "/api/v1/async-schedule", json=payload
        )

    assert response.status_code == 422
    assert "Request payload modified" in response.json().get("detail", "")


@pytest.mark.asyncio
async def test_task_status_polling_pending(
    authenticated_client: AsyncClient,
):
    """Test task status polling for a pending task returns PENDING state."""
    test_task_id = str(uuid.uuid4())

    mock_result = MagicMock()
    mock_result.state = "PENDING"

    with (
        patch(
            "celery_app.celery_app.backend.get_key_for_task", return_value="key"
        ),
        patch("celery_app.celery_app.backend.get", return_value=b"meta"),
        patch("routes.task_status.AsyncResult", return_value=mock_result),
    ):
        response = await authenticated_client.get(
            f"/api/v1/tasks/{test_task_id}"
        )

    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == test_task_id
    assert data["status"] == "PENDING"


@pytest.mark.asyncio
async def test_task_status_polling_success(
    authenticated_client: AsyncClient,
):
    """Test task status polling for a completed task returns SUCCESS."""
    test_task_id = str(uuid.uuid4())
    expected_result = {"status": "scheduled", "event_id": "google-event-777"}

    mock_result = MagicMock()
    mock_result.state = "SUCCESS"
    mock_result.result = expected_result

    with (
        patch(
            "celery_app.celery_app.backend.get_key_for_task", return_value="key"
        ),
        patch("celery_app.celery_app.backend.get", return_value=b"meta"),
        patch("routes.task_status.AsyncResult", return_value=mock_result),
    ):
        response = await authenticated_client.get(
            f"/api/v1/tasks/{test_task_id}"
        )

    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == test_task_id
    assert data["status"] == "SUCCESS"
    assert data["result"] == expected_result


@pytest.mark.asyncio
async def test_task_status_polling_failure(
    authenticated_client: AsyncClient,
):
    """Test task status polling for a failed task returns FAILURE."""
    test_task_id = str(uuid.uuid4())

    mock_result = MagicMock()
    mock_result.state = "FAILURE"
    mock_result.failed.return_value = True
    mock_result.result = Exception("Google Calendar API quota exceeded")

    with (
        patch(
            "celery_app.celery_app.backend.get_key_for_task", return_value="key"
        ),
        patch("celery_app.celery_app.backend.get", return_value=b"meta"),
        patch("routes.task_status.AsyncResult", return_value=mock_result),
    ):
        response = await authenticated_client.get(
            f"/api/v1/tasks/{test_task_id}"
        )

    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == test_task_id
    assert data["status"] == "FAILURE"
    assert "Google Calendar API quota exceeded" in data["error"]


@pytest.mark.asyncio
async def test_task_status_invalid_uuid(authenticated_client: AsyncClient):
    """Test task status polling with non-UUID returns 422 error."""
    response = await authenticated_client.get(
        "/api/v1/tasks/invalid-non-uuid-string"
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_task_status_not_found(authenticated_client: AsyncClient):
    """Test task status polling for non-existent task returns 404."""
    non_existent_task_id = str(uuid.uuid4())

    with (
        patch(
            "celery_app.celery_app.backend.get_key_for_task", return_value="key"
        ),
        patch("celery_app.celery_app.backend.get", return_value=None),
    ):
        response = await authenticated_client.get(
            f"/api/v1/tasks/{non_existent_task_id}"
        )

    assert response.status_code == 404
    assert (
        f"Task ID '{non_existent_task_id}' was not found."
        in response.json().get("detail", "")
    )
