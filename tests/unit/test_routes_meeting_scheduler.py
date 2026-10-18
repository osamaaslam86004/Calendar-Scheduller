"""
Unit tests for routes/meeting_scheduler.py
"""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import get_current_user
from messaging.bus import PayloadMismatchError
from models.user_db import UserDB
from routes.meeting_scheduler import router


@pytest.mark.unit
class TestMeetingScheduler:
    @pytest.fixture
    def mock_user(self):
        """Provide a mock authenticated UserDB model."""
        return UserDB(id=15, email="organizer@example.com", name="Organizer")

    @pytest.fixture
    def app(self, mock_user):
        """Build test application mounting meeting_scheduler router with user dependency override."""
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user] = lambda: mock_user
        return app

    @pytest.fixture
    def client(self, app):
        """Provide TestClient for meeting scheduling endpoints."""
        return TestClient(app)

    @patch("routes.meeting_scheduler.get_shared_ledger")
    @patch("routes.meeting_scheduler.atomic_consume")
    @patch("routes.meeting_scheduler.get_redis_client")
    def test_async_schedule_success_via_message_bus(
        self,
        mock_get_redis,
        mock_atomic_consume,
        mock_get_ledger,
        app,
        client,
    ):
        """Verify successful meeting scheduling via message bus idempotency pipeline."""
        mock_redis = AsyncMock()
        mock_get_redis.return_value = mock_redis
        mock_atomic_consume.return_value = (True, 1, 50)

        # Mock Anomaly Manager (circuit closed)
        mock_flag_mgr = AsyncMock()
        mock_flag_mgr.is_circuit_open.return_value = False
        app.state.flag_anomaly_manager = mock_flag_mgr

        # Mock Message Bus
        mock_bus = AsyncMock()
        mock_bus.process_with_retry.return_value = {
            "task_id": "bus_task_guid_123",
            "status": "PENDING",
        }
        app.state.message_bus = mock_bus

        payload = {"request_text": "Schedule 1:1 sync with Bob on Friday at 3pm"}
        response = client.post("/api/v1/async-schedule", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == "bus_task_guid_123"
        assert data["status"] == "PENDING"
        assert mock_bus.process_with_retry.called
        assert mock_get_ledger.called

    @patch("routes.meeting_scheduler.get_shared_ledger")
    @patch("routes.meeting_scheduler.execute_calendar_schedule_task")
    @patch("routes.meeting_scheduler.atomic_consume")
    @patch("routes.meeting_scheduler.get_redis_client")
    def test_async_schedule_success_fallback_without_bus(
        self,
        mock_get_redis,
        mock_atomic_consume,
        mock_celery_task,
        mock_get_ledger,
        app,
        client,
    ):
        """Verify direct celery enqueue fallback when message bus is not initialized on app state."""
        mock_redis = AsyncMock()
        mock_get_redis.return_value = mock_redis
        mock_atomic_consume.return_value = (True, 1, 30)

        app.state.flag_anomaly_manager = None
        app.state.message_bus = None

        mock_celery_task.delay.return_value = MagicMock(id="celery_direct_task_456")

        payload = {"request_text": "Schedule demo on Monday at 10am"}
        response = client.post("/api/v1/async-schedule", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == "celery_direct_task_456"
        assert data["status"] == "PENDING"
        mock_celery_task.delay.assert_called_once_with(
            user_id=15, request_text="Schedule demo on Monday at 10am"
        )

    def test_async_schedule_circuit_breaker_open_raises_503(self, app, client):
        """Verify circuit breaker trips with HTTP 503 Service Unavailable when system health is degraded."""
        mock_flag_mgr = AsyncMock()
        mock_flag_mgr.is_circuit_open.return_value = True
        mock_flag_mgr.get_health_score.return_value = 45.5
        app.state.flag_anomaly_manager = mock_flag_mgr

        payload = {"request_text": "Schedule sync meeting"}
        response = client.post("/api/v1/async-schedule", json=payload)

        assert response.status_code == 503
        assert "System degraded (health 45.5/100)" in response.json()["detail"]

    @patch("routes.meeting_scheduler.atomic_consume")
    @patch("routes.meeting_scheduler.get_redis_client")
    def test_async_schedule_rate_limit_exceeded_raises_429(
        self, mock_get_redis, mock_atomic_consume, app, client
    ):
        """Verify dual-bucket consumption rejection returns HTTP 429 Too Many Requests."""
        mock_get_redis.return_value = AsyncMock()
        mock_atomic_consume.return_value = (False, 10, 50000)

        app.state.flag_anomaly_manager = None

        payload = {"request_text": "Schedule urgent meeting"}
        response = client.post("/api/v1/async-schedule", json=payload)

        assert response.status_code == 429
        assert "Rate limit exceeded" in response.json()["detail"]

    @patch("routes.meeting_scheduler.atomic_consume")
    @patch("routes.meeting_scheduler.get_redis_client")
    def test_async_schedule_idempotency_payload_tampering_raises_422(
        self, mock_get_redis, mock_atomic_consume, app, client
    ):
        """Verify payload mismatch error on idempotency key reuse returns HTTP 422 Unprocessable Entity."""
        mock_get_redis.return_value = AsyncMock()
        mock_atomic_consume.return_value = (True, 1, 20)
        app.state.flag_anomaly_manager = None

        mock_bus = AsyncMock()
        mock_bus.process_with_retry.side_effect = PayloadMismatchError(
            "Fingerprint collision with modified body"
        )
        app.state.message_bus = mock_bus

        payload = {"request_text": "Schedule tampered meeting request"}
        response = client.post("/api/v1/async-schedule", json=payload)

        assert response.status_code == 422
        assert "Request payload modified" in response.json()["detail"]

    @patch("routes.meeting_scheduler.get_shared_ledger")
    @patch("routes.meeting_scheduler.execute_calendar_schedule_task")
    @patch("routes.meeting_scheduler.atomic_consume")
    @patch("routes.meeting_scheduler.get_redis_client")
    def test_async_schedule_bus_exception_falls_back_to_direct_enqueue(
        self,
        mock_get_redis,
        mock_atomic_consume,
        mock_celery_task,
        mock_get_ledger,
        app,
        client,
    ):
        """Verify transient message bus exception seamlessly falls back to direct task enqueue."""
        mock_get_redis.return_value = AsyncMock()
        mock_atomic_consume.return_value = (True, 1, 25)
        app.state.flag_anomaly_manager = None

        mock_bus = AsyncMock()
        mock_bus.process_with_retry.side_effect = RuntimeError(
            "Redis connection dropped"
        )
        app.state.message_bus = mock_bus

        mock_celery_task.delay.return_value = MagicMock(id="fallback_celery_task_789")

        payload = {"request_text": "Schedule team standup"}
        response = client.post("/api/v1/async-schedule", json=payload)

        assert response.status_code == 200
        assert response.json()["task_id"] == "fallback_celery_task_789"
        assert mock_celery_task.delay.called

    def test_async_schedule_validation_error_on_empty_payload(self, client):
        """Verify validation error with HTTP 422 when request body is empty."""
        response = client.post("/api/v1/async-schedule", json={})
        assert response.status_code == 422

    def test_async_schedule_unauthenticated_without_user(self):
        """Verify unauthenticated user cannot schedule a meeting without credentials."""
        from starlette.middleware.sessions import SessionMiddleware

        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key="test-secret")
        app.include_router(router)
        client = TestClient(app)

        response = client.post(
            "/api/v1/async-schedule",
            json={"request_text": "Meeting with team"},
        )
        assert response.status_code == 401
