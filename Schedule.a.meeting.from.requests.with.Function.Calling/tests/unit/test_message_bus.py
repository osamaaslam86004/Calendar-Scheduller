"""
Unit tests for messaging/bus.py (AsyncMessageBus, Idempotency, Fingerprinting, and Retries)
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from messaging.bus import (
    AsyncMessageBus,
    IdempotencyError,
    PayloadMismatchError,
)


@pytest.mark.unit
class TestAsyncMessageBus:
    @pytest.fixture
    def mock_redis(self):
        """Provide a mock async Redis client."""
        client = AsyncMock()
        return client

    @pytest.fixture
    def bus(self, mock_redis):
        """Provide an AsyncMessageBus instance configured with mock Redis and test TTL."""
        return AsyncMessageBus(redis_client=mock_redis, ttl_seconds=1800)

    # -------------------------------------------------------------------------
    # 1. Payload Fingerprinting
    # -------------------------------------------------------------------------

    def test_compute_fingerprint_deterministic_key_ordering(self):
        """Verify fingerprint is invariant to dict key insertion order."""
        payload_1 = {"user_id": 42, "action": "CREATE_MEETING", "tz": "UTC"}
        payload_2 = {"tz": "UTC", "action": "CREATE_MEETING", "user_id": 42}

        fp_1 = AsyncMessageBus.compute_fingerprint(payload_1)
        fp_2 = AsyncMessageBus.compute_fingerprint(payload_2)

        assert fp_1 == fp_2
        assert len(fp_1) == 64  # SHA-256 hex string length

    def test_compute_fingerprint_different_payloads(self):
        """Verify distinct payloads yield different SHA-256 fingerprints."""
        payload_1 = {"user_id": 1, "topic": "Sprint Review"}
        payload_2 = {"user_id": 1, "topic": "Sprint Retrospective"}

        fp_1 = AsyncMessageBus.compute_fingerprint(payload_1)
        fp_2 = AsyncMessageBus.compute_fingerprint(payload_2)

        assert fp_1 != fp_2

    def test_compute_fingerprint_unicode_characters(self):
        """Verify non-ASCII / Unicode characters are preserved and hashed deterministically."""
        payload = {"greeting": "こんにちは", "emoji": "📅", "user": "José"}
        fp = AsyncMessageBus.compute_fingerprint(payload)

        assert isinstance(fp, str)
        assert len(fp) == 64
        # Re-computing on identical data produces identical hash
        assert fp == AsyncMessageBus.compute_fingerprint(payload)

    def test_compute_fingerprint_nested_structures(self):
        """Verify nested dictionaries and lists are serialized and hashed consistently."""
        payload_a = {
            "meta": {"nested_id": 10, "flags": ["sync", "audit"]},
            "count": 5,
        }
        payload_b = {
            "count": 5,
            "meta": {"flags": ["sync", "audit"], "nested_id": 10},
        }

        fp_a = AsyncMessageBus.compute_fingerprint(payload_a)
        fp_b = AsyncMessageBus.compute_fingerprint(payload_b)

        assert fp_a == fp_b

    # -------------------------------------------------------------------------
    # 2. Idempotency Checks & State Management
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_check_idempotency_new_key_initializes_pending_state(
        self, bus, mock_redis
    ):
        """Verify unseen idempotency key stores pending marker with fingerprint and returns (True, None)."""
        mock_redis.get.return_value = None

        payload = {"meeting_id": "m_100", "organizer": "alice"}
        is_new, cached = await bus.check_idempotency("key_001", payload)

        assert is_new is True
        assert cached is None

        mock_redis.get.assert_awaited_once_with("idempotency:key_001")
        mock_redis.setex.assert_awaited_once()

        call_args = mock_redis.setex.await_args[0]
        assert call_args[0] == "idempotency:key_001"
        assert call_args[1] == 1800  # ttl_seconds

        stored_json = json.loads(call_args[2])
        assert stored_json["status"] == "pending"
        assert stored_json["fingerprint"] == AsyncMessageBus.compute_fingerprint(
            payload
        )

    @pytest.mark.anyio
    async def test_check_idempotency_existing_key_completed_response(
        self, bus, mock_redis
    ):
        """Verify key reuse with identical payload returns (False, cached_result)."""
        payload = {"meeting_id": "m_100", "organizer": "alice"}
        fp = AsyncMessageBus.compute_fingerprint(payload)

        cached_data = {
            "status": "completed",
            "fingerprint": fp,
            "response": {"task_id": "celery_task_88", "status": "SUCCESS"},
        }
        mock_redis.get.return_value = json.dumps(cached_data)

        is_new, cached = await bus.check_idempotency("key_001", payload)

        assert is_new is False
        assert cached == {"task_id": "celery_task_88", "status": "SUCCESS"}
        mock_redis.setex.assert_not_called()

    @pytest.mark.anyio
    async def test_check_idempotency_existing_key_pending_marker(self, bus, mock_redis):
        """Verify identical key check during pending execution returns (False, None)."""
        payload = {"request": "schedule_1"}
        fp = AsyncMessageBus.compute_fingerprint(payload)

        pending_data = {
            "status": "pending",
            "fingerprint": fp,
        }
        mock_redis.get.return_value = json.dumps(pending_data)

        is_new, cached = await bus.check_idempotency("key_pending_01", payload)

        assert is_new is False
        assert cached is None

    @pytest.mark.anyio
    async def test_check_idempotency_bytes_response_from_redis(self, bus, mock_redis):
        """Verify binary bytes returned from Redis client are decoded and parsed cleanly."""
        payload = {"user_id": 99}
        fp = AsyncMessageBus.compute_fingerprint(payload)

        completed_bytes = json.dumps(
            {"status": "completed", "fingerprint": fp, "response": "ok"}
        ).encode("utf-8")
        mock_redis.get.return_value = completed_bytes

        is_new, cached = await bus.check_idempotency("key_bytes_01", payload)

        assert is_new is False
        assert cached == "ok"

    @pytest.mark.anyio
    async def test_check_idempotency_payload_mismatch_raises_error(
        self, bus, mock_redis
    ):
        """Verify reusing an idempotency key with modified payload raises PayloadMismatchError."""
        original_payload = {"user_id": 5, "duration": 30}
        tampered_payload = {"user_id": 5, "duration": 60}

        original_fp = AsyncMessageBus.compute_fingerprint(original_payload)

        stored_data = {
            "status": "completed",
            "fingerprint": original_fp,
            "response": {"id": "orig_res"},
        }
        mock_redis.get.return_value = json.dumps(stored_data)

        with pytest.raises(PayloadMismatchError) as exc_info:
            await bus.check_idempotency("key_tampered", tampered_payload)

        assert "reused with a modified payload" in str(exc_info.value)

    @pytest.mark.anyio
    async def test_check_idempotency_corrupted_data_raises_payload_mismatch_error(
        self, bus, mock_redis
    ):
        """Verify corrupted/unparsable JSON in Redis raises PayloadMismatchError."""
        mock_redis.get.return_value = b"{corrupted_invalid_json..."

        with pytest.raises(PayloadMismatchError) as exc_info:
            await bus.check_idempotency("key_bad_json", {"data": 1})

        assert "Corrupted idempotency data" in str(exc_info.value)

    # -------------------------------------------------------------------------
    # 3. Response Caching
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_cache_response_success(self, bus, mock_redis):
        """Verify cache_response updates stored state to completed and writes back response."""
        fp = AsyncMessageBus.compute_fingerprint({"msg": "data"})
        initial_data = {"status": "pending", "fingerprint": fp}
        mock_redis.get.return_value = json.dumps(initial_data)

        result_payload = {"task_id": "task_abc", "confirmed": True}
        await bus.cache_response("key_cache_01", result_payload)

        mock_redis.setex.assert_awaited_once()
        call_args = mock_redis.setex.await_args[0]
        assert call_args[0] == "idempotency:key_cache_01"
        assert call_args[1] == 1800

        updated_obj = json.loads(call_args[2])
        assert updated_obj["status"] == "completed"
        assert updated_obj["fingerprint"] == fp
        assert updated_obj["response"] == result_payload

    @pytest.mark.anyio
    async def test_cache_response_corrupted_data_handled_gracefully(
        self, bus, mock_redis
    ):
        """Verify cache_response does not raise an exception if existing data is corrupt."""
        mock_redis.get.return_value = "invalid-json-content"

        # Should complete gracefully without raising
        await bus.cache_response("key_corrupt", {"some": "result"})
        mock_redis.setex.assert_not_called()

    # -------------------------------------------------------------------------
    # 4. Jittered Exponential Backoff
    # -------------------------------------------------------------------------

    def test_compute_backoff_delay_within_bounds(self):
        """Verify backoff delays fall within [0, min(max_delay, base_delay * 2^attempt)]."""
        base_delay = 1.0
        max_delay = 60.0

        for attempt in range(6):
            max_possible = min(max_delay, base_delay * (2**attempt))
            for _ in range(20):
                delay = AsyncMessageBus.compute_backoff_delay(
                    attempt=attempt, base_delay=base_delay, max_delay=max_delay
                )
                assert 0.0 <= delay <= max_possible

    def test_compute_backoff_delay_capped_at_max_delay(self):
        """Verify high attempt numbers are strictly capped by max_delay."""
        base_delay = 2.0
        max_delay = 10.0

        for _ in range(20):
            # 2.0 * 2^8 = 512, which far exceeds max_delay of 10.0
            delay = AsyncMessageBus.compute_backoff_delay(
                attempt=8, base_delay=base_delay, max_delay=max_delay
            )
            assert 0.0 <= delay <= max_delay

    @patch("messaging.bus.random.uniform")
    def test_compute_backoff_delay_formula_invokes_random_uniform(self, mock_uniform):
        """Verify random.uniform is invoked with (0, min(max_delay, base_delay * 2^attempt))."""
        mock_uniform.return_value = 3.5

        delay = AsyncMessageBus.compute_backoff_delay(
            attempt=2, base_delay=1.0, max_delay=30.0
        )

        assert delay == 3.5
        # base_delay * 2^2 = 4.0; min(30.0, 4.0) = 4.0
        mock_uniform.assert_called_once_with(0, 4.0)

    # -------------------------------------------------------------------------
    # 5. Retry Processing (`process_with_retry`)
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_process_with_retry_success_first_attempt_without_idempotency(
        self, bus, mock_redis
    ):
        """Verify handler execution succeeds on first attempt without idempotency key."""
        mock_handler = AsyncMock(return_value={"status": "done"})

        result = await bus.process_with_retry(
            handler=mock_handler,
            payload={"event": "ping"},
            idempotency_key=None,
        )

        assert result == {"status": "done"}
        mock_handler.assert_awaited_once_with({"event": "ping"})
        mock_redis.get.assert_not_called()

    @pytest.mark.anyio
    async def test_process_with_retry_success_first_attempt_with_idempotency(
        self, bus, mock_redis
    ):
        """Verify handler execution succeeds with idempotency, checking and caching response."""
        mock_redis.get.return_value = None  # Key is new

        mock_handler = AsyncMock(return_value={"job_id": "job_123"})
        payload = {"data": "sync"}

        result = await bus.process_with_retry(
            handler=mock_handler,
            payload=payload,
            idempotency_key="idem_key_exec",
        )

        assert result == {"job_id": "job_123"}
        mock_handler.assert_awaited_once_with(payload)
        # Check idempotency initialization + cache_response calls
        assert mock_redis.setex.await_count >= 1

    @pytest.mark.anyio
    async def test_process_with_retry_returns_cached_response_without_handler_execution(
        self, bus, mock_redis
    ):
        """Verify key with completed cached response returns immediately without executing handler."""
        payload = {"task": "process_doc"}
        fp = AsyncMessageBus.compute_fingerprint(payload)

        cached_obj = {
            "status": "completed",
            "fingerprint": fp,
            "response": {"task_id": "cached_res_99"},
        }
        mock_redis.get.return_value = json.dumps(cached_obj)

        mock_handler = AsyncMock()

        result = await bus.process_with_retry(
            handler=mock_handler,
            payload=payload,
            idempotency_key="key_cached_hit",
        )

        assert result == {"task_id": "cached_res_99"}
        mock_handler.assert_not_called()

    @pytest.mark.anyio
    async def test_process_with_retry_payload_mismatch_fails_immediately(
        self, bus, mock_redis
    ):
        """Verify payload tampering error prevents handler execution and raises PayloadMismatchError."""
        stored_obj = {
            "status": "completed",
            "fingerprint": "different_sha256_fingerprint",
            "response": {"result": "old"},
        }
        mock_redis.get.return_value = json.dumps(stored_obj)

        mock_handler = AsyncMock()

        with pytest.raises(PayloadMismatchError):
            await bus.process_with_retry(
                handler=mock_handler,
                payload={"different": "payload"},
                idempotency_key="key_tampered_exec",
            )

        mock_handler.assert_not_called()

    @pytest.mark.anyio
    @patch("messaging.bus.asyncio.sleep")
    async def test_process_with_retry_transient_timeout_retries_and_succeeds(
        self, mock_sleep, bus
    ):
        """Verify transient asyncio.TimeoutError retries with backoff and succeeds on subsequent try."""
        mock_handler = AsyncMock()
        mock_handler.side_effect = [
            asyncio.TimeoutError("Upstream timeout"),
            {"status": "recovered"},
        ]

        result = await bus.process_with_retry(
            handler=mock_handler,
            payload={"action": "recheck"},
            idempotency_key=None,
            max_retries=3,
            base_delay=1.0,
        )

        assert result == {"status": "recovered"}
        assert mock_handler.await_count == 2
        mock_sleep.assert_awaited_once()

    @pytest.mark.anyio
    @patch("messaging.bus.asyncio.sleep")
    async def test_process_with_retry_transient_http_statuses_retries(
        self, mock_sleep, bus
    ):
        """Verify transient HTTP statuses (429, 500, 502, 503, 504) trigger backoff retries."""
        for transient_code in (429, 500, 502, 503, 504):
            mock_sleep.reset_mock()
            error_obj = Exception(f"HTTP {transient_code}")
            error_obj.status_code = transient_code

            mock_handler = AsyncMock()
            mock_handler.side_effect = [error_obj, {"code": transient_code, "ok": True}]

            result = await bus.process_with_retry(
                handler=mock_handler,
                payload={"test": transient_code},
                idempotency_key=None,
                max_retries=2,
            )

            assert result == {"code": transient_code, "ok": True}
            assert mock_handler.await_count == 2
            mock_sleep.assert_awaited_once()

    @pytest.mark.anyio
    @patch("messaging.bus.asyncio.sleep")
    async def test_process_with_retry_max_retries_exceeded_raises_last_error(
        self, mock_sleep, bus
    ):
        """Verify exceeding max_retries on transient failure raises the terminal exception."""
        error_503 = Exception("Service Unavailable")
        error_503.status_code = 503

        mock_handler = AsyncMock(side_effect=error_503)

        with pytest.raises(Exception) as exc_info:
            await bus.process_with_retry(
                handler=mock_handler,
                payload={"req": "failing"},
                idempotency_key=None,
                max_retries=3,
            )

        assert exc_info.value == error_503
        assert mock_handler.await_count == 3
        # Should have slept between attempts (attempts 0 and 1, but not after final attempt 2)
        assert mock_sleep.await_count == 2

    @pytest.mark.anyio
    @patch("messaging.bus.asyncio.sleep")
    async def test_process_with_retry_non_transient_error_raises_immediately(
        self, mock_sleep, bus
    ):
        """Verify non-transient errors (e.g. ValueError, HTTP 400, HTTP 404) fail immediately without retries."""
        error_400 = Exception("Bad Request")
        error_400.status_code = 400

        mock_handler = AsyncMock(side_effect=error_400)

        with pytest.raises(Exception) as exc_info:
            await bus.process_with_retry(
                handler=mock_handler,
                payload={"bad": "param"},
                idempotency_key=None,
                max_retries=5,
            )

        assert exc_info.value == error_400
        assert mock_handler.await_count == 1
        mock_sleep.assert_not_called()
