"""
Unit tests for routes/dlq_admin.py
"""

import json
from unittest.mock import MagicMock, patch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from routes.dlq_admin import router
from utilities.security import build_tenant_context


@pytest.mark.unit
class TestDLQAdmin:
    @pytest.fixture
    def app(self):
        """Create a dedicated FastAPI test application mounting the dlq_admin router."""
        app = FastAPI()
        app.include_router(router)
        return app

    @pytest.fixture
    def client(self, app):
        """Provide a TestClient for testing DLQ endpoints."""
        return TestClient(app)

    @pytest.fixture
    def admin_headers(self):
        """Provide valid admin authorization headers."""
        return {
            "X-Admin-Key": settings.security.app_master_key,
        }

    @pytest.fixture
    def tenant_token_and_headers(self, admin_headers):
        """Provide valid admin auth headers paired with a scoped tenant JWT."""
        tenant_id = "tenant_test_10"
        jwt_token = build_tenant_context(
            tenant_id=tenant_id,
            scopes=["dlq:replay"],
            bounds={"allowed_operations": ["replay"]},
        )
        headers = dict(admin_headers)
        headers["X-Tenant-JWT"] = jwt_token
        headers["X-Idempotency-Key"] = "idem_key_unit_123"
        return tenant_id, headers

    def test_require_admin_unauthorized_when_missing(self, client):
        """Verify endpoints reject requests with HTTP 401 when admin authentication header is omitted."""
        response = client.get("/admin/dlq/")
        assert response.status_code == 401
        assert response.json()["detail"] == "Admin auth required"

    def test_require_admin_unauthorized_when_invalid(self, client):
        """Verify endpoints reject requests with HTTP 401 when admin key is incorrect."""
        response = client.get(
            "/admin/dlq/", headers={"X-Admin-Key": "invalid_master_key"}
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Admin auth required"

    def test_require_admin_authorized_via_bearer(self, client):
        """Verify admin authorization succeeds using Authorization: Bearer <key>."""
        with patch("routes.dlq_admin.redis_client") as mock_r:
            mock_r.lrange.return_value = []
            response = client.get(
                "/admin/dlq/",
                headers={"Authorization": f"Bearer {settings.security.app_master_key}"},
            )
            assert response.status_code == 200
            assert response.json() == []

    @patch("routes.dlq_admin.celery_app")
    @patch("routes.dlq_admin.redis_client")
    def test_replay_dlq_message_success(
        self, mock_r, mock_celery, client, tenant_token_and_headers
    ):
        """Verify successful DLQ message replay increments replay_count, sends celery task, and audits."""
        tenant_id, headers = tenant_token_and_headers
        msg_id = "task_dlq_001"
        raw_msg = json.dumps(
            {
                "task_id": msg_id,
                "user_id": tenant_id,
                "request_text": "Schedule sync meeting tomorrow at 3pm",
                "replay_count": 0,
            }
        )

        mock_r.get.return_value = None  # Idempotency check pass
        mock_r.set.return_value = True  # Lock acquisition pass
        mock_r.lrange.return_value = [raw_msg]

        mock_async_res = MagicMock(id="new_celery_task_999")
        mock_celery.send_task.return_value = mock_async_res

        payload = {"message_id": msg_id}
        response = client.post("/admin/dlq/replay", json=payload, headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert data["original_task_id"] == msg_id
        assert data["new_task_id"] == "new_celery_task_999"
        assert data["status"] == "requeued"

        # Verify lock cleanup in finally block
        mock_r.delete.assert_called_with(f"dlq:lock:{msg_id}")
        assert mock_celery.send_task.called

    @patch("routes.dlq_admin.redis_client")
    def test_replay_dlq_message_idempotency_conflict(
        self, mock_r, client, tenant_token_and_headers
    ):
        """Verify duplicate replay requests with an active/completed idempotency key return HTTP 409."""
        _, headers = tenant_token_and_headers
        mock_r.get.return_value = "already_completed_task"

        response = client.post(
            "/admin/dlq/replay",
            json={"message_id": "task_dlq_dup"},
            headers=headers,
        )
        assert response.status_code == 409
        assert "Replay active or completed" in response.json()["detail"]

    @patch("routes.dlq_admin.redis_client")
    def test_replay_dlq_message_lock_conflict(
        self, mock_r, client, tenant_token_and_headers
    ):
        """Verify concurrent replay requests return HTTP 423 when locked."""
        _, headers = tenant_token_and_headers
        mock_r.get.return_value = None  # Idempotency check pass
        mock_r.set.return_value = False  # Lock acquisition failed (NX=True failed)

        response = client.post(
            "/admin/dlq/replay",
            json={"message_id": "task_dlq_locked"},
            headers=headers,
        )
        assert response.status_code == 423
        assert "Message currently locked" in response.json()["detail"]

    @patch("routes.dlq_admin.redis_client")
    def test_replay_dlq_message_not_found(
        self, mock_r, client, tenant_token_and_headers
    ):
        """Verify replay returns HTTP 404 when target message is absent from Redis DLQ."""
        _, headers = tenant_token_and_headers
        mock_r.get.return_value = None
        mock_r.set.return_value = True
        mock_r.lrange.return_value = []

        response = client.post(
            "/admin/dlq/replay",
            json={"message_id": "task_missing_404"},
            headers=headers,
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "DLQ message not found"

    @patch("routes.dlq_admin.redis_client")
    def test_replay_dlq_message_tenant_mismatch(
        self, mock_r, client, tenant_token_and_headers
    ):
        """Verify replay returns HTTP 403 when authenticated tenant does not own the DLQ message."""
        tenant_id, headers = tenant_token_and_headers
        msg_id = "task_dlq_other"
        raw_msg = json.dumps(
            {
                "task_id": msg_id,
                "user_id": "different_tenant_owner_99",
                "request_text": "Private schedule request",
                "replay_count": 0,
            }
        )
        mock_r.get.return_value = None
        mock_r.set.return_value = True
        mock_r.lrange.return_value = [raw_msg]

        response = client.post(
            "/admin/dlq/replay",
            json={"message_id": msg_id},
            headers=headers,
        )
        assert response.status_code == 403
        assert "Tenant mismatch" in response.json()["detail"]

    @patch("routes.dlq_admin.adaptive_throttling")
    @patch("routes.dlq_admin.redis_client")
    def test_replay_dlq_message_quarantined_after_max_replays(
        self, mock_r, mock_adaptive, client, tenant_token_and_headers
    ):
        """Verify message with replay_count >= 3 is moved to quarantine set and returns HTTP 400."""
        tenant_id, headers = tenant_token_and_headers
        msg_id = "task_dlq_exhausted"
        raw_msg = json.dumps(
            {
                "task_id": msg_id,
                "user_id": tenant_id,
                "request_text": "Bad request crashing workers",
                "replay_count": 3,
            }
        )
        mock_r.get.return_value = None
        mock_r.set.return_value = True
        mock_r.lrange.return_value = [raw_msg]

        response = client.post(
            "/admin/dlq/replay",
            json={"message_id": msg_id},
            headers=headers,
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Moved to quarantine."
        assert mock_r.sadd.called
        assert mock_r.lrem.called

    @patch("routes.dlq_admin.get_calendar_engine")
    @patch("routes.dlq_admin.redis_client")
    def test_dlq_dry_run_success(
        self, mock_r, mock_get_engine, client, tenant_token_and_headers
    ):
        """Verify dry-run performs parsing and HMAC calculation without triggering side-effects."""
        tenant_id, headers = tenant_token_and_headers
        msg_id = "task_dlq_dry"
        raw_msg = json.dumps(
            {
                "task_id": msg_id,
                "user_id": tenant_id,
                "request_text": "Coffee meeting on Friday at 10am",
                "replay_count": 1,
            }
        )
        mock_r.lrange.return_value = [raw_msg]

        mock_engine = MagicMock()
        mock_func_call = MagicMock()
        mock_func_call.model_dump.return_value = {
            "summary": "Coffee meeting",
            "start": {"date_time": "2026-10-16T10:00:00Z"},
        }
        mock_engine.extract_calendar_function.return_value = mock_func_call
        mock_get_engine.return_value = mock_engine

        response = client.post(
            "/admin/dlq/dry-run",
            json={"message_id": msg_id},
            headers=headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["message_id"] == msg_id
        assert data["cached"] is False
        assert data["parsed_function"]["summary"] == "Coffee meeting"
        assert data["hmac_signature"] is not None

    @patch("routes.dlq_admin.redis_client")
    def test_list_dlq_entries(self, mock_r, client, admin_headers):
        """Verify listing DLQ entries parses JSON records from Redis list."""
        valid_entry = {
            "task_id": "task_1",
            "request_text": "Sync",
            "user_id": 10,
            "error": "Execution failure",
            "created_at": "2026-10-13T00:00:00Z",
        }
        mock_r.lrange.return_value = [json.dumps(valid_entry), "invalid_json_bytes"]

        response = client.get("/admin/dlq/", headers=admin_headers)
        assert response.status_code == 200
        entries = response.json()
        assert len(entries) == 1
        assert entries[0]["task_id"] == "task_1"

    @patch("routes.dlq_admin.celery_app")
    @patch("routes.dlq_admin.redis_client")
    def test_requeue_single_entry(self, mock_r, mock_celery, client, admin_headers):
        """Verify requeueing a single entry re-submits task and removes from Redis."""
        raw_msg = json.dumps(
            {"task_id": "task_req_1", "user_id": 1, "request_text": "Sync"}
        )
        mock_r.lrange.return_value = [raw_msg]
        mock_celery.send_task.return_value = MagicMock(id="new_id_1")

        response = client.post(
            "/admin/dlq/requeue?task_id=task_req_1", headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json()["new_task_id"] == "new_id_1"
        assert mock_r.lrem.called

    @patch("routes.dlq_admin.redis_client")
    def test_requeue_single_entry_not_found(self, mock_r, client, admin_headers):
        """Verify requeueing a non-existent task returns HTTP 404."""
        mock_r.lrange.return_value = []
        response = client.post(
            "/admin/dlq/requeue?task_id=task_missing", headers=admin_headers
        )
        assert response.status_code == 404

    @patch("routes.dlq_admin.celery_app")
    @patch("routes.dlq_admin.redis_client")
    def test_requeue_all_entries(self, mock_r, mock_celery, client, admin_headers):
        """Verify requeue_all processes multiple items and returns count summary."""
        raw1 = json.dumps({"task_id": "t1", "user_id": 1, "request_text": "text1"})
        raw2 = json.dumps({"task_id": "t2", "user_id": 2, "request_text": "text2"})
        mock_r.lrange.return_value = [raw1, raw2]
        mock_celery.send_task.side_effect = [
            MagicMock(id="new_t1"),
            MagicMock(id="new_t2"),
        ]

        response = client.post("/admin/dlq/requeue_all", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert len(data["results"]) == 2

    @patch("routes.dlq_admin.redis_client")
    def test_delete_dlq_entry_success(self, mock_r, client, admin_headers):
        """Verify deletion of an entry removes it from Redis list."""
        raw = json.dumps({"task_id": "t_del", "user_id": 1})
        mock_r.lrange.return_value = [raw]

        response = client.delete("/admin/dlq/t_del", headers=admin_headers)
        assert response.status_code == 200
        assert response.json() == {"deleted": "t_del"}
        assert mock_r.lrem.called

    @patch("routes.dlq_admin.redis_client")
    def test_delete_dlq_entry_not_found(self, mock_r, client, admin_headers):
        """Verify deleting non-existent entry returns HTTP 404."""
        mock_r.lrange.return_value = []
        response = client.delete("/admin/dlq/t_nonexistent", headers=admin_headers)
        assert response.status_code == 404
