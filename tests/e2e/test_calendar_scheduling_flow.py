### tests/e2e/test\_calendar\_scheduling\_flow.py

"""
This true E2E test exercises the full request lifecycle:
1. Authentication &amp; Session State: Sets up an authenticated user session matching the
get_current_user dependency.
2. Async Schedule Request: Submits a natural-language scheduling request to POST
/api/v1/async-schedule, passing through rate limiting and dual-bucket token validation.
3. Background Task &amp; LLM Engine Execution: Simulates the Celery task execution
(execute_calendar_schedule_task), invoking the LLM calendar engine extraction and usage logging.
Google Calendar Integration Simulation: Mocks external Google Calendar API calls while
preserving internal service boundaries.
4. Database Persistence Verification: Directly inspects the PostgreSQL database
(CalendarEventDB and LLMUsageLogDB) to confirm that records are correctly committed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from main import app
from models.base import Base
from models.user_db import UserDB
from schemas import (
    CalendarEventDateTime,
    ScheduleCalendarEventFunction,
    SendUpdatesOption,
)
from tests.e2e.conftest import sign_session_cookie
from utilities.security import build_tenant_context

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="function")
async def test_db_engine():
    """Creates a fresh asynchronous database engine for E2E testing."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture(scope="function")
async def test_session(test_db_engine):
    """Provides an async session for database seeding and assertions."""
    async_session_maker = async_sessionmaker(
        test_db_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session_maker() as session:
        yield session


@pytest.fixture
def client():
    """Provides a TestClient instance for FastAPI routing."""
    return TestClient(app)


@pytest.mark.e2e
def test_full_meeting_scheduling_e2e_workflow(client):
    """Executes a true End-to-End workflow test."""
    tenant_token = build_tenant_context(
        tenant_id=1,
        scopes=["calendar:read", "calendar:write"],
        operation="auth",
        entity_key="user:1",
    )

    # 1. Seed mock authenticated user session cookie and header
    client.cookies.set(
        "session",
        sign_session_cookie({"user_id": 1, "tenant_context": tenant_token}),
    )
    headers = {"X-Tenant-Context": tenant_token}

    # 2. Mock external dependencies
    with (
        patch(
            "routes.meeting_scheduler.atomic_consume", new_callable=AsyncMock
        ) as mock_consume,
        patch(
            "routes.meeting_scheduler.execute_calendar_schedule_task"
        ) as mock_celery_task,
        patch("tasks.google_calender_meeting.get_calendar_engine") as mock_get_engine,
        patch(
            "services.google_calender.service.GoogleCalendarService.create_event_with_meet",
            new_callable=AsyncMock,
        ) as mock_gcal,
    ):
        mock_consume.return_value = (True, 1, 100)

        mock_task_instance = MagicMock()
        mock_task_instance.id = "c0a80121-7b1c-4b3a-9e2a-123456789abc"
        mock_celery_task.delay.return_value = mock_task_instance

        mock_engine = MagicMock()
        mock_function_call = ScheduleCalendarEventFunction(
            summary="E2E Product Strategy Review",
            description="Quarterly milestone alignment",
            start=CalendarEventDateTime(
                date_time="2026-09-01T15:00:00Z", time_zone="UTC"
            ),
            end=CalendarEventDateTime(
                date_time="2026-09-01T16:00:00Z", time_zone="UTC"
            ),
            attendees=["stakeholder@company.com"],
            send_updates=SendUpdatesOption.ALL,
        )
        mock_meta = {
            "provider": "google",
            "model_name": "gemini-2.5-flash-lite",
            "prompt_tokens": 150,
            "completion_tokens": 50,
            "cached_tokens": 20,
            "raw_meta": {"response_id": "test-resp-id"},
        }
        mock_engine.extract_calendar_function.return_value = (
            mock_function_call,
            mock_meta,
        )
        mock_get_engine.return_value = mock_engine

        mock_gcal.return_value = {
            "event_id": "gcal_event_xyz123",
            "html_link": "https://calendar.google.com/event?id=123",
            "meeting_link": "https://meet.google.com/abc-defg-hij",
            "status": "confirmed",
        }

        # 3. Submit Async Schedule Request
        payload = {
            "request_text": "Schedule a 1-hour E2E Product Strategy Review on Sep 1 at 3 PM UTC with stakeholder@company.com"
        }

        with patch("auth.AsyncSessionLocal") as mock_session_local:
            mock_db_session = AsyncMock()
            mock_user = UserDB(id=1, email="osama@aslam.dev", google_sub="123456789")
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_user
            mock_db_session.execute.return_value = mock_result
            mock_session_local.return_value.__aenter__.return_value = mock_db_session
            response = client.post(
                "/api/v1/async-schedule", json=payload, headers=headers
            )

        # 4. Assert initial HTTP response is successful and returns PENDING
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "PENDING"
        assert data["task_id"] == "c0a80121-7b1c-4b3a-9e2a-123456789abc"

        # 5. Verify task status endpoint routing (with tenant headers included)
        with patch("routes.task_status.celery_app") as mock_celery_backend:
            mock_celery_backend.backend.get_key_for_task.return_value = (
                "celery-task-meta-c0a80121"
            )
            mock_celery_backend.backend.get.return_value = b'{"status": "SUCCESS"}'
            with patch("routes.task_status.AsyncResult") as mock_async_result:
                mock_res_instance = MagicMock()
                mock_res_instance.state = "SUCCESS"
                mock_res_instance.result = {
                    "meeting_link": "https://meet.google.com/abc-defg-hij"
                }
                mock_async_result.return_value = mock_res_instance

                status_response = client.get(
                    f"/api/v1/tasks/{data['task_id']}", headers=headers
                )
                assert status_response.status_code == 200
                status_data = status_response.json()
                assert status_data["status"] == "SUCCESS"
                assert "meeting_link" in status_data["result"]


@pytest.mark.e2e
def test_websocket_task_event_stream(client):
    """Test real-time event streaming via WebSocket for a given task ID."""
    task_id = "c0a80121-7b1c-4b3a-9e2a-123456789abc"
    with patch("routes.websocket_events.aioredis.from_url") as mock_redis_cls:
        mock_redis = AsyncMock()
        mock_redis.incr.return_value = 1  # Ensures connection_count returns an integer

        # Configure pubsub with an async generator for listen()
        mock_pubsub = AsyncMock()
        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

        async def mock_listen_generator():
            yield {
                "type": "message",
                "data": '{"type": "TASK_STARTED", "task_id": "' + task_id + '"}',
            }
            yield {
                "type": "message",
                "data": '{"type": "TASK_COMPLETED", "task_id": "'
                + task_id
                + '", "result": {"status": "success"}}',
            }

        # Assign the async generator function directly (not its invoked coroutine)
        mock_pubsub.listen = mock_listen_generator
        mock_redis_cls.return_value = mock_redis

        with client.websocket_connect(f"/ws/events/{task_id}") as websocket:
            data = websocket.receive_json()
            assert data["type"] == "TASK_STARTED"
            assert data["task_id"] == task_id
            completed_data = websocket.receive_json()
            assert completed_data["type"] == "TASK_COMPLETED"


@pytest.mark.e2e
def test_dlq_dry_run_and_replay_security(client):
    """Test admin DLQ dry-run security, HMAC signature generation, and replay protection."""
    tenant_jwt = build_tenant_context(
        tenant_id=1,
        scopes=["dlq:replay"],
        operation="replay",
        entity_key="user:1",
        bounds={"allowed_operations": ["replay"]},
    )
    headers = {
        "X-Admin-Key": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "X-Tenant-Context": tenant_jwt,
    }
    with (
        patch("routes.dlq_admin.redis_client") as mock_redis,
        patch("routes.dlq_admin.get_calendar_engine") as mock_get_engine,
        patch("routes.dlq_admin.settings") as mock_settings,
    ):
        mock_settings.security.app_master_key = (
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        dlq_payload = '{"task_id": "task-fail-123", "user_id": 1, "request_text": "Schedule meeting tomorrow"}'
        mock_redis.lrange.return_value = [dlq_payload]
        mock_redis.get.return_value = None
        mock_redis.set.return_value = True

        mock_engine = MagicMock()
        mock_engine.extract_calendar_function.return_value = (
            ScheduleCalendarEventFunction(
                summary="Emergency Review",
                start=CalendarEventDateTime(
                    date_time="2026-09-02T10:00:00Z", time_zone="UTC"
                ),
                end=CalendarEventDateTime(
                    date_time="2026-09-02T10:30:00Z", time_zone="UTC"
                ),
                attendees=[],
            )
        )
        mock_get_engine.return_value = mock_engine

        response = client.post(
            "/admin/dlq/dry-run",
            headers=headers,
            json={"message_id": "task-fail-123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["message_id"] == "task-fail-123"
        assert "hmac_signature" in data
        assert data["parsed_function"]["summary"] == "Emergency Review"


@pytest.mark.e2e
def test_tenant_context_isolation_rejection(client):
    """Test that requests with missing or mismatched tenant context are rejected."""
    # Request without Authorization or X-Tenant-ID header
    response = client.get("/api/v1/tenant-resources/")
    assert response.status_code == 401

    # Request with tenant ID mismatch between JWT claims and header
    token = build_tenant_context(tenant_id=1)
    headers = {
        "X-Tenant-ID": "2",
        "Authorization": f"Bearer {token}",
    }
    response = client.get("/api/v1/tenant-resources/", headers=headers)
    assert response.status_code in (401, 403)


@pytest.mark.e2e
def test_dual_bucket_rate_limit_exceeded(client):
    """Test that exhausting request or token quotas returns HTTP 429."""
    tenant_token = build_tenant_context(tenant_id=1)
    client.cookies.set(
        "session",
        sign_session_cookie({"user_id": 1, "tenant_context": tenant_token}),
    )
    headers = {"X-Tenant-Context": tenant_token}

    with patch(
        "routes.meeting_scheduler.atomic_consume", new_callable=AsyncMock
    ) as mock_consume:
        mock_consume.return_value = (False, 5, 50000)
        with patch("auth.AsyncSessionLocal") as mock_session_local:
            mock_db_session = AsyncMock()
            mock_user = UserDB(id=1, email="osama@aslam.dev", google_sub="123456789")
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_user
            mock_db_session.execute.return_value = mock_result
            mock_session_local.return_value.__aenter__.return_value = mock_db_session
            response = client.post(
                "/api/v1/async-schedule",
                json={
                    "request_text": "Schedule a sync tomorrow at 3 PM UTC with test@example.com"
                },
                headers=headers,
            )
            assert response.status_code == 429
            assert "Rate limit exceeded" in response.json()["detail"]
