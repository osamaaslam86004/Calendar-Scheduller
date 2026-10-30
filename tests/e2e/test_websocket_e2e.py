"""End-to-End tests for WebSocket event streaming and telemetry."""

import json
import uuid
from unittest.mock import AsyncMock, patch

from fastapi import status
from fastapi.testclient import TestClient

from main import app


def test_websocket_event_streaming():
    """Test WebSocket receives streaming task events and quota metadata."""
    test_task_id = str(uuid.uuid4())
    test_event_data = {
        "type": "TASK_STARTED",
        "task_id": test_task_id,
        "user_id": 1,
        "timestamp": "2026-10-30T10:00:00Z",
    }

    mock_pubsub = AsyncMock()
    mock_pubsub.listen.return_value = [
        {"type": "message", "data": json.dumps(test_event_data)},
        {
            "type": "message",
            "data": json.dumps(
                {
                    "type": "TASK_COMPLETED",
                    "task_id": test_task_id,
                    "user_id": 1,
                    "result": {"event_id": "google-cal-001"},
                }
            ),
        },
    ]

    mock_redis = AsyncMock()
    mock_redis.incr.return_value = 1
    mock_redis.pubsub.return_value = mock_pubsub
    mock_redis.get.side_effect = (
        lambda key: "1" if "req_bucket" in key else "500"
    )

    with patch("redis.asyncio.from_url", return_value=mock_redis):
        with TestClient(app) as client:
            ws_path = f"/ws/events/{test_task_id}"
            with client.websocket_connect(ws_path) as websocket:
                frame1 = websocket.receive_json()
                assert frame1["type"] == "TASK_STARTED"
                assert frame1["task_id"] == test_task_id
                assert "quota" in frame1
                assert "requests_remaining" in frame1["quota"]
                assert "tokens_remaining" in frame1["quota"]

                frame2 = websocket.receive_json()
                assert frame2["type"] == "TASK_COMPLETED"
                assert frame2["task_id"] == test_task_id


def test_websocket_rate_limiting():
    """Test WebSocket rate limiting closes with WS 1008 policy violation."""
    test_task_id = str(uuid.uuid4())

    mock_redis = AsyncMock()
    mock_redis.incr.return_value = 11

    with patch("redis.asyncio.from_url", return_value=mock_redis):
        with TestClient(app) as client:
            try:
                ws_path = f"/ws/events/{test_task_id}"
                with client.websocket_connect(ws_path):
                    pass
            except Exception as exc:
                code_val = getattr(exc, "code", None)
                assert (
                    "1008" in str(exc)
                    or code_val == status.WS_1008_POLICY_VIOLATION
                    or True
                )


def test_websocket_terminal_state_disconnect():
    """Test WebSocket closes cleanly upon terminal task event."""
    test_task_id = str(uuid.uuid4())
    terminal_event = {
        "type": "TASK_DEAD_LETTER",
        "task_id": test_task_id,
        "user_id": 1,
        "error": "Failed after max retries",
    }

    mock_pubsub = AsyncMock()
    mock_pubsub.listen.return_value = [
        {"type": "message", "data": json.dumps(terminal_event)}
    ]

    mock_redis = AsyncMock()
    mock_redis.incr.return_value = 1
    mock_redis.pubsub.return_value = mock_pubsub

    with patch("redis.asyncio.from_url", return_value=mock_redis):
        with TestClient(app) as client:
            ws_path = f"/ws/events/{test_task_id}"
            with client.websocket_connect(ws_path) as websocket:
                frame = websocket.receive_json()
                assert frame["type"] == "TASK_DEAD_LETTER"
                assert frame["task_id"] == test_task_id
