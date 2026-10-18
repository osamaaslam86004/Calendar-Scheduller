import json
from unittest.mock import MagicMock

import pytest

from tasks import dlq


def test_push_dead_letter_rpush_and_payload(monkeypatch):
    # Arrange: patch tenant context builder and redis client
    fake_tenant = {"tenant": "fake"}
    monkeypatch.setattr(dlq, "build_tenant_context", lambda **kwargs: fake_tenant)
    fake_rpush = MagicMock()
    monkeypatch.setattr(dlq, "redis_client", MagicMock(rpush=fake_rpush))

    # Act
    dlq.push_dead_letter("task-123", 42, "request text", "some error")

    # Assert rpush called with DLQ key and JSON payload
    fake_rpush.assert_called_once()
    call_args = fake_rpush.call_args[0]
    assert call_args[0] == dlq.settings.redis.dlq_key
    payload = json.loads(call_args[1])
    assert payload["task_id"] == "task-123"
    assert payload["user_id"] == 42
    assert payload["request_text"] == "request text"
    assert payload["error"] == "some error"
    assert payload["tenant_context"] == fake_tenant
    # created_at is an ISO-like timestamp
    assert "T" in payload["created_at"]
