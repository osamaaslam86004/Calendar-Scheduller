"""Integration tests for DLQ replay, idempotency locks, and quarantine."""

import json
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

from config.settings import settings
from main import app
from utilities.security import build_tenant_context


@pytest.mark.integration
class TestDLQAdminIntegration:
    @pytest.fixture
    def client_and_redis(self, sync_redis, async_redis):
        # Patch dlq.redis_client, routes.dlq_admin.redis_client, and cache
        with patch("dlq.redis_client", sync_redis), patch(
            "routes.dlq_admin.redis_client", sync_redis
        ), patch("config.cache.get_redis_client", return_value=async_redis):
            test_client = TestClient(app)

            # Seed a dummy DLQ message into settings.redis.dlq_key
            msg_id = "dlq_msg_404"
            user_id = "u_12"
            payload = {"meeting_id": "m_990", "action": "CREATE_INVITE", "user_id": user_id}
            dlq_record = {
                "task_id": msg_id,
                "user_id": user_id,
                "replay_count": 0,
                "request_text": "Schedule sync meeting with Team",
                "payload": payload,
            }
            sync_redis.rpush(settings.redis.dlq_key, json.dumps(dlq_record))

            tenant_jwt = build_tenant_context(
                tenant_id=user_id,
                scopes=["calendar:read", "calendar:write", "dlq:replay"],
                operation="dlq:replay",
                entity_key=f"user:{user_id}",
                bounds={"user_id": user_id, "allowed_operations": ["replay"]},
            )

            yield test_client, sync_redis, msg_id, user_id, payload, tenant_jwt

    def test_successful_idempotent_replay(self, client_and_redis):
        """Verify DLQ replay execution using a valid X-Idempotency-Key and authorized tenant token."""
        client, r, msg_id, user_id, payload, tenant_jwt = client_and_redis
        headers = {
            "X-Idempotency-Key": "idem_key_unique_001",
            "X-Tenant-ID": user_id,
            "X-Tenant-JWT": tenant_jwt,
            "Authorization": f"Bearer {tenant_jwt}",
            "X-Admin-Key": settings.security.app_master_key,
        }

        with patch("celery_app.celery_app.send_task") as mock_send_task:
            mock_send_task.return_value.id = "new_task_999"
            response = client.post(
                "/admin/dlq/replay",
                json={"message_id": msg_id, "override_payload": payload},
                headers=headers,
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "requeued"
            assert data["new_task_id"] == "new_task_999"

            # Replay count should be incremented to 1 in DLQ
            raw_entries = r.lrange(settings.redis.dlq_key, 0, -1)
            updated = [json.loads(e) for e in raw_entries if json.loads(e).get("task_id") == msg_id]
            assert len(updated) == 1
            assert updated[0]["replay_count"] == 1

            # Idempotency key should be recorded in Redis
            assert r.get("idempotency:dlq:idem_key_unique_001") == msg_id

    def test_idempotency_key_reuse_rejected_with_409(self, client_and_redis):
        """Verify that reusing an active/completed idempotency key returns HTTP 409 Conflict."""
        client, r, msg_id, user_id, payload, tenant_jwt = client_and_redis
        headers = {
            "X-Idempotency-Key": "idem_key_unique_002",
            "X-Tenant-ID": user_id,
            "X-Tenant-JWT": tenant_jwt,
            "Authorization": f"Bearer {tenant_jwt}",
            "X-Admin-Key": settings.security.app_master_key,
        }

        with patch("celery_app.celery_app.send_task") as mock_send_task:
            mock_send_task.return_value.id = "new_task_100"
            # First Call -> Successfully process replay
            resp1 = client.post(
                "/admin/dlq/replay",
                json={"message_id": msg_id, "override_payload": payload},
                headers=headers,
            )
            assert resp1.status_code == 200

            # Second Call with SAME Idempotency Key -> Must be rejected with 409 Conflict
            resp2 = client.post(
                "/admin/dlq/replay",
                json={"message_id": msg_id, "override_payload": payload},
                headers=headers,
            )
            assert resp2.status_code == 409
            assert "replay active or completed" in resp2.json()["detail"].lower()

    def test_excessive_replays_trigger_quarantine(self, client_and_redis):
        """Verify that attempting to replay a message with replay_count >= 3 moves it to quarantine."""
        client, r, msg_id, user_id, payload, tenant_jwt = client_and_redis

        # Seed a message with replay_count = 3
        quarantine_msg_id = "dlq_msg_quarantine_01"
        quarantine_record = {
            "task_id": quarantine_msg_id,
            "user_id": user_id,
            "replay_count": 3,
            "request_text": "Excessive retry request",
            "payload": payload,
        }
        r.rpush(settings.redis.dlq_key, json.dumps(quarantine_record))

        headers = {
            "X-Idempotency-Key": "idem_key_quarantine_001",
            "X-Tenant-ID": user_id,
            "X-Tenant-JWT": tenant_jwt,
            "Authorization": f"Bearer {tenant_jwt}",
            "X-Admin-Key": settings.security.app_master_key,
        }

        response = client.post(
            "/admin/dlq/replay",
            json={"message_id": quarantine_msg_id},
            headers=headers,
        )

        # Must return HTTP 400 Moved to quarantine
        assert response.status_code == 400
        assert "quarantine" in response.json()["detail"].lower()

        # Message must now be in dlq:quarantine set and removed from active list
        quarantine_members = r.smembers("dlq:quarantine")
        assert any(quarantine_msg_id in m for m in quarantine_members)

        active_entries = r.lrange(settings.redis.dlq_key, 0, -1)
        assert not any(json.loads(e).get("task_id") == quarantine_msg_id for e in active_entries)
