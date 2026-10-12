"""
Unit tests for routes/websocket_events.py
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import WebSocket, WebSocketDisconnect, status

from routes.websocket_events import task_events


@pytest.mark.unit
class TestWebSocketEvents:
    @pytest.fixture
    def mock_websocket(self):
        """Provide a mock WebSocket connection with client host."""
        ws = AsyncMock(spec=WebSocket)
        ws.client = MagicMock(host="192.168.1.50")
        return ws

    @pytest.fixture
    def mock_redis_client(self):
        """Provide an AsyncMock representing the async Redis connection."""
        r = AsyncMock()
        r.incr.return_value = 1
        r.expire.return_value = True
        r.close.return_value = None
        return r

    @pytest.mark.anyio
    async def test_websocket_rate_limit_exceeded_closes_connection(
        self, mock_websocket, mock_redis_client
    ):
        """Verify client opening > 10 connections per minute gets closed with WS 1008 Policy Violation."""
        mock_redis_client.incr.return_value = 11  # Exceeds rate limit threshold of 10

        with patch(
            "routes.websocket_events.aioredis.from_url",
            return_value=mock_redis_client,
        ):
            await task_events(mock_websocket, "task_ws_rate_limit")

        assert mock_websocket.accept.call_count == 1
        mock_websocket.close.assert_called_once_with(
            code=status.WS_1008_POLICY_VIOLATION
        )
        assert mock_redis_client.close.called
        assert not mock_redis_client.pubsub.called

    @pytest.mark.anyio
    async def test_websocket_stream_messages_and_terminates_on_completed(
        self, mock_websocket, mock_redis_client
    ):
        """Verify normal stream yields started and completed events, then exits and cleans up."""
        mock_redis_client.incr.return_value = 1

        mock_pubsub = AsyncMock()
        msg_start = {
            "type": "message",
            "data": json.dumps({"type": "TASK_STARTED", "task_id": "task_stream_01"}),
        }
        msg_done = {
            "type": "message",
            "data": json.dumps(
                {
                    "type": "TASK_COMPLETED",
                    "task_id": "task_stream_01",
                    "result": "success",
                }
            ),
        }

        async def listen_generator():
            yield msg_start
            yield msg_done

        mock_pubsub.listen = listen_generator
        mock_redis_client.pubsub = MagicMock(return_value=mock_pubsub)

        with patch(
            "routes.websocket_events.aioredis.from_url",
            return_value=mock_redis_client,
        ):
            await task_events(mock_websocket, "task_stream_01")

        assert mock_websocket.accept.call_count == 2
        assert mock_websocket.send_json.call_count == 2
        mock_websocket.send_json.assert_any_call(json.loads(msg_start["data"]))
        mock_websocket.send_json.assert_any_call(json.loads(msg_done["data"]))

        mock_pubsub.unsubscribe.assert_called_once_with("task_events:task_stream_01")
        assert mock_pubsub.close.called
        assert mock_redis_client.close.called

    @pytest.mark.anyio
    async def test_websocket_quota_augmentation_for_user_event(
        self, mock_websocket, mock_redis_client
    ):
        """Verify events containing user_id are enriched with live token & request quota calculations."""
        mock_redis_client.incr.return_value = 2

        mock_redis_client.get.side_effect = lambda key: (
            "2" if "req_bucket" in key else "15000"
        )

        mock_pubsub = AsyncMock()
        user_event = {
            "type": "message",
            "data": json.dumps(
                {
                    "type": "TASK_COMPLETED",
                    "task_id": "task_user_quota",
                    "user_id": 42,
                }
            ),
        }

        async def listen_generator():
            yield user_event

        mock_pubsub.listen = listen_generator
        mock_redis_client.pubsub = MagicMock(return_value=mock_pubsub)

        with patch(
            "routes.websocket_events.aioredis.from_url",
            return_value=mock_redis_client,
        ):
            await task_events(mock_websocket, "task_user_quota")

        assert mock_websocket.send_json.call_count == 1
        sent_payload = mock_websocket.send_json.call_args[0][0]

        assert "quota" in sent_payload
        assert sent_payload["quota"]["requests_remaining"] == 3  # 5 - 2
        assert sent_payload["quota"]["tokens_remaining"] == 35000  # 50000 - 15000
        assert sent_payload["quota"]["token_window_seconds"] == 3600

    @pytest.mark.anyio
    async def test_websocket_breaks_on_dead_letter(
        self, mock_websocket, mock_redis_client
    ):
        """Verify terminal state TASK_DEAD_LETTER breaks the pubsub loop cleanly."""
        mock_redis_client.incr.return_value = 1

        mock_pubsub = AsyncMock()
        msg_dead = {
            "type": "message",
            "data": json.dumps(
                {
                    "type": "TASK_DEAD_LETTER",
                    "task_id": "task_dlq_01",
                    "error": "Quarantined",
                }
            ),
        }

        async def listen_generator():
            yield msg_dead

        mock_pubsub.listen = listen_generator
        mock_redis_client.pubsub = MagicMock(return_value=mock_pubsub)

        with patch(
            "routes.websocket_events.aioredis.from_url",
            return_value=mock_redis_client,
        ):
            await task_events(mock_websocket, "task_dlq_01")

        assert mock_websocket.send_json.call_count == 1
        assert mock_pubsub.unsubscribe.called
        assert mock_redis_client.close.called

    @pytest.mark.anyio
    async def test_websocket_ignores_non_message_types(
        self, mock_websocket, mock_redis_client
    ):
        """Verify non-message pubsub control frames are skipped."""
        mock_redis_client.incr.return_value = 1

        mock_pubsub = AsyncMock()
        ctrl_frame = {"type": "subscribe", "channel": "task_events:t1"}
        msg_terminal = {
            "type": "message",
            "data": json.dumps({"type": "TASK_COMPLETED", "task_id": "t1"}),
        }

        async def listen_generator():
            yield ctrl_frame
            yield msg_terminal

        mock_pubsub.listen = listen_generator
        mock_redis_client.pubsub = MagicMock(return_value=mock_pubsub)

        with patch(
            "routes.websocket_events.aioredis.from_url",
            return_value=mock_redis_client,
        ):
            await task_events(mock_websocket, "t1")

        assert mock_websocket.send_json.call_count == 1

    @pytest.mark.anyio
    async def test_websocket_client_disconnect_handled_gracefully(
        self, mock_websocket, mock_redis_client
    ):
        """Verify WebSocketDisconnect exception cleanly unsubscribes and closes connection."""
        mock_redis_client.incr.return_value = 1

        mock_pubsub = AsyncMock()
        msg = {
            "type": "message",
            "data": json.dumps({"type": "TASK_PROGRESS", "task_id": "t_dc"}),
        }

        async def listen_generator():
            yield msg

        mock_pubsub.listen = listen_generator
        mock_redis_client.pubsub = MagicMock(return_value=mock_pubsub)
        mock_websocket.send_json.side_effect = WebSocketDisconnect()

        with patch(
            "routes.websocket_events.aioredis.from_url",
            return_value=mock_redis_client,
        ):
            await task_events(mock_websocket, "t_dc")

        # Finally block still executes
        assert mock_pubsub.unsubscribe.called
        assert mock_pubsub.close.called
        assert mock_redis_client.close.called
