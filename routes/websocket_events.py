# routes/websocket_events.py
# This file contains the WebSocket endpoint for streaming task events to clients in real-time.

# Rate Limiting Strategy:
# For WebSockets, slowapi decorators cannot be applied directly to standard connection handlers.
# Implement an initial handshaking check against Redis prior to accepting the connection.

import json
import redis.asyncio as aioredis

from fastapi import APIRouter, WebSocket, status
from fastapi import WebSocketDisconnect

from config.settings import settings
from config.limiter import get_identifier

router = APIRouter()


@router.websocket("/ws/events/{task_id}")
async def task_events(
    websocket: WebSocket,
    task_id: str,
):
    """
    Now your agent can connect once:

    ws://localhost:8000/ws/events/<task_id>
    and receive

    {
      "type": "TASK_STARTED",
      "task_id": "..."
    }
    """

    await websocket.accept()

    # Initialize Async Redis Client
    redis_client = aioredis.from_url(
        settings.redis.async_url,
        db=settings.redis.async_db,
        decode_responses=True,
    )

    # Custom WebSocket Rate Limiting (e.g., max 10 WS connections per minute per client)
    client_key = f"ws_limit:{get_identifier(websocket)}"
    connection_count = await redis_client.incr(client_key)
    if connection_count == 1:
        await redis_client.expire(client_key, 60)

    if connection_count > 10:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        await redis_client.close()
        return

    await websocket.accept()

    pubsub = redis_client.pubsub()

    channel = f"task_events:{task_id}"

    await pubsub.subscribe(channel)

    try:
        # Non-blocking async loop using pubsub.listen()
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue

            data = json.loads(message["data"])
            # If event includes user_id, augment with remaining quota metadata
            user_id = data.get("user_id")
            if user_id is not None:
                try:
                    REQUEST_LIMIT = 5
                    REQUEST_WINDOW = 60
                    TOKEN_LIMIT = 50000
                    TOKEN_WINDOW = 3600

                    import time

                    now = int(time.time())
                    req_min = now // REQUEST_WINDOW
                    token_hour = now // TOKEN_WINDOW

                    req_key = f"req_bucket:{user_id}:{req_min}"
                    token_key = f"token_used:{user_id}:{token_hour}"

                    current_req = await redis_client.get(req_key) or 0
                    current_req = int(current_req)
                    req_remaining = max(0, REQUEST_LIMIT - current_req)

                    current_token = await redis_client.get(token_key) or 0
                    current_token = int(current_token)
                    token_remaining = max(0, TOKEN_LIMIT - current_token)

                    data["quota"] = {
                        "requests_remaining": req_remaining,
                        "tokens_remaining": token_remaining,
                        "token_window_seconds": TOKEN_WINDOW,
                    }
                except Exception:
                    # Non-fatal: just send the event without quota
                    pass

            await websocket.send_json(data)

            # Close socket loop on terminal task states
            if data.get("type") in {"TASK_COMPLETED", "TASK_DEAD_LETTER"}:
                break

    except WebSocketDisconnect:
        pass

    finally:
        # Graceful async cleanup
        await pubsub.unsubscribe(channel)
        await pubsub.close()
        await redis_client.close()
