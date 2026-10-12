"""
Unit tests for routes/task_status.py
"""

from unittest.mock import MagicMock, patch
import uuid
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.task_status import router


@pytest.mark.unit
class TestTaskStatus:
    @pytest.fixture
    def app(self):
        """Build test application mounting task_status router."""
        app = FastAPI()
        app.include_router(router)
        return app

    @pytest.fixture
    def client(self, app):
        """Provide TestClient for task status endpoints."""
        return TestClient(app)

    @pytest.fixture
    def sample_uuid(self):
        """Provide a valid UUID v4 task identifier."""
        return str(uuid.uuid4())

    @patch("routes.task_status.AsyncResult")
    @patch("routes.task_status.celery_app")
    def test_get_task_status_pending(
        self, mock_celery, mock_async_result_cls, client, sample_uuid
    ):
        """Verify task in PENDING state returns expected response model."""
        mock_celery.backend.get_key_for_task.return_value = (
            f"celery-task-meta-{sample_uuid}"
        )
        mock_celery.backend.get.return_value = b'{"status": "PENDING"}'

        mock_res = MagicMock()
        mock_res.state = "PENDING"
        mock_res.failed.return_value = False
        mock_async_result_cls.return_value = mock_res

        response = client.get(f"/api/v1/tasks/{sample_uuid}")
        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == sample_uuid
        assert data["status"] == "PENDING"
        assert data["result"] is None
        assert data["error"] is None

    @patch("routes.task_status.AsyncResult")
    @patch("routes.task_status.celery_app")
    def test_get_task_status_started(
        self, mock_celery, mock_async_result_cls, client, sample_uuid
    ):
        """Verify task in STARTED state returns expected response model."""
        mock_celery.backend.get_key_for_task.return_value = (
            f"celery-task-meta-{sample_uuid}"
        )
        mock_celery.backend.get.return_value = b'{"status": "STARTED"}'

        mock_res = MagicMock()
        mock_res.state = "STARTED"
        mock_res.failed.return_value = False
        mock_async_result_cls.return_value = mock_res

        response = client.get(f"/api/v1/tasks/{sample_uuid}")
        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == sample_uuid
        assert data["status"] == "STARTED"

    @patch("routes.task_status.AsyncResult")
    @patch("routes.task_status.celery_app")
    def test_get_task_status_success_with_payload(
        self, mock_celery, mock_async_result_cls, client, sample_uuid
    ):
        """Verify successful task returns result payload."""
        mock_celery.backend.get_key_for_task.return_value = (
            f"celery-task-meta-{sample_uuid}"
        )
        mock_celery.backend.get.return_value = b'{"status": "SUCCESS"}'

        mock_res = MagicMock()
        mock_res.state = "SUCCESS"
        mock_res.result = {
            "google_calendar_event_id": "g_event_888",
            "link": "https://meet.google.com/abc",
        }
        mock_res.failed.return_value = False
        mock_async_result_cls.return_value = mock_res

        response = client.get(f"/api/v1/tasks/{sample_uuid}")
        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == sample_uuid
        assert data["status"] == "SUCCESS"
        assert data["result"]["google_calendar_event_id"] == "g_event_888"

    @patch("routes.task_status.AsyncResult")
    @patch("routes.task_status.celery_app")
    def test_get_task_status_failure(
        self, mock_celery, mock_async_result_cls, client, sample_uuid
    ):
        """Verify failed task captures error message."""
        mock_celery.backend.get_key_for_task.return_value = (
            f"celery-task-meta-{sample_uuid}"
        )
        mock_celery.backend.get.return_value = b'{"status": "FAILURE"}'

        mock_res = MagicMock()
        mock_res.state = "FAILURE"
        mock_res.failed.return_value = True
        mock_res.result = "OAuthTokenExpiredException: Token expired and refresh failed"
        mock_async_result_cls.return_value = mock_res

        response = client.get(f"/api/v1/tasks/{sample_uuid}")
        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == sample_uuid
        assert data["status"] == "FAILURE"
        assert "OAuthTokenExpiredException" in data["error"]

    @patch("routes.task_status.AsyncResult")
    @patch("routes.task_status.celery_app")
    def test_get_task_status_custom_state(
        self, mock_celery, mock_async_result_cls, client, sample_uuid
    ):
        """Verify handling of custom or intermediate Celery states like RETRY."""
        mock_celery.backend.get_key_for_task.return_value = (
            f"celery-task-meta-{sample_uuid}"
        )
        mock_celery.backend.get.return_value = b'{"status": "RETRY"}'

        mock_res = MagicMock()
        mock_res.state = "RETRY"
        mock_res.failed.return_value = False
        mock_async_result_cls.return_value = mock_res

        response = client.get(f"/api/v1/tasks/{sample_uuid}")
        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == sample_uuid
        assert data["status"] == "RETRY"

    @patch("routes.task_status.celery_app")
    def test_get_task_status_not_found_in_backend(
        self, mock_celery, client, sample_uuid
    ):
        """Verify task not present in Celery Redis backend returns HTTP 404."""
        mock_celery.backend.get_key_for_task.return_value = (
            f"celery-task-meta-{sample_uuid}"
        )
        mock_celery.backend.get.return_value = None

        response = client.get(f"/api/v1/tasks/{sample_uuid}")
        assert response.status_code == 404
        assert f"Task ID '{sample_uuid}' was not found" in response.json()["detail"]

    def test_get_task_status_invalid_uuid_format(self, client):
        """Verify non-UUID path parameters are rejected by regex validation with HTTP 422."""
        # Non-UUID string
        res1 = client.get("/api/v1/tasks/not-a-valid-uuid")
        assert res1.status_code == 422

        # Too short UUID-like string
        res2 = client.get("/api/v1/tasks/12345678-1234-1234-1234-1234567890")
        assert res2.status_code == 422
