import json
from unittest.mock import MagicMock

from tasks import events


def test_publish_task_event_uses_correlation(monkeypatch):
    fake_corr = "corr-123"
    monkeypatch.setattr(events, "get_current_correlation_id", lambda: fake_corr)
    fake_publish = MagicMock()
    monkeypatch.setattr(events, "redis_client", MagicMock(publish=fake_publish))
    fake_append = MagicMock()
    monkeypatch.setattr(events, "append_audit_event", fake_append)

    ev = {"type": "created", "user_id": "u1"}
    events.publish_task_event("task1", ev)

    fake_publish.assert_called_once()
    channel, payload = fake_publish.call_args[0]
    assert channel == "task_events:task1"
    ev_published = json.loads(payload)
    assert ev_published["correlation_id"] == fake_corr

    fake_append.assert_called_once()
    called_args, called_kwargs = fake_append.call_args
    # append_audit_event is called using keyword args in production
    assert called_kwargs.get("event_type") == "created"
    assert called_kwargs.get("request_id") == "task1"
    assert called_kwargs.get("correlation_id") == fake_corr


def test_publish_task_event_preserves_existing_correlation(monkeypatch):
    fake_publish = MagicMock()
    monkeypatch.setattr(events, "redis_client", MagicMock(publish=fake_publish))
    fake_append = MagicMock()
    monkeypatch.setattr(events, "append_audit_event", fake_append)

    ev = {"type": "updated", "user_id": "u2", "correlation_id": "pre-corr"}
    events.publish_task_event("task2", ev)

    fake_publish.assert_called_once()
    _, payload = fake_publish.call_args[0]
    ev_published = json.loads(payload)
    assert ev_published["correlation_id"] == "pre-corr"
    # append_audit_event called with provided correlation
    _, called_kwargs = fake_append.call_args
    assert called_kwargs["correlation_id"] == "pre-corr"
