"""Async Messaging Bus with idempotency, fingerprinting, and jittered backoff.

Features:
- SHA-256 fingerprinting of payloads bound to idempotency keys (detects tampering).
- Jittered exponential backoff for transient failures.
- Idempotency tracking via Redis to detect key reuse attacks.
- HTTP 422 response on payload mismatch (modified JSON with same key).
"""

import asyncio
import hashlib
import json
import logging
import random
from typing import Any, Callable, Dict, Optional, Tuple

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class IdempotencyError(Exception):
    """Base exception for idempotency violations."""

    pass


class PayloadMismatchError(IdempotencyError):
    """Raised when an idempotency key is reused with a different payload."""

    pass


class AsyncMessageBus:
    """Async message bus with idempotency and payload integrity checking.

    Attributes:
        redis: Redis async client for storing fingerprints.
        ttl_seconds: TTL for idempotency key-fingerprint pairs (default 3600s = 1 hour).
    """

    def __init__(self, redis_client: Redis, ttl_seconds: int = 3600):
        self.redis = redis_client
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def compute_fingerprint(payload: Dict[str, Any]) -> str:
        """Compute SHA-256 fingerprint of a JSON payload.

        Args:
            payload: Dictionary to fingerprint.

        Returns:
            Hex-encoded SHA-256 hash.
        """
        json_str = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(json_str.encode("utf-8")).hexdigest()

    async def check_idempotency(
        self, idempotency_key: str, payload: Dict[str, Any]
    ) -> Tuple[bool, Optional[Any]]:
        """Check if an idempotency key has been seen before with a different payload.

        Args:
            idempotency_key: Unique key for this request.
            payload: Request payload to fingerprint.

        Returns:
            (is_new, cached_result): If is_new=True, key is new (not seen before).
                If is_new=False, key was seen before; cached_result is the stored response
                (or None if original request is still pending).

        Raises:
            PayloadMismatchError: If key exists but fingerprint differs (tampering detected).
        """
        current_fingerprint = self.compute_fingerprint(payload)
        cache_key = f"idempotency:{idempotency_key}"
        stored_data = await self.redis.get(cache_key)

        if stored_data is None:
            # Key is new; store fingerprint and None (pending state)
            pending_marker = json.dumps(
                {"status": "pending", "fingerprint": current_fingerprint}
            )
            await self.redis.setex(cache_key, self.ttl_seconds, pending_marker)
            return True, None

        # Key exists; check fingerprint
        try:
            stored_obj = json.loads(
                stored_data.decode("utf-8")
                if isinstance(stored_data, bytes)
                else stored_data
            )
        except Exception:
            logger.exception(
                "Failed to decode stored idempotency data for key %s", idempotency_key
            )
            raise PayloadMismatchError(
                f"Corrupted idempotency data for key {idempotency_key}"
            )

        stored_fingerprint = stored_obj.get("fingerprint", "")
        if stored_fingerprint != current_fingerprint:
            logger.warning(
                "Idempotency key reuse attack detected: %s (fingerprints differ)",
                idempotency_key,
            )
            raise PayloadMismatchError(
                f"Idempotency key {idempotency_key} was reused with a modified payload"
            )

        # Fingerprint matches; return cached response if available
        cached_response = stored_obj.get("response")
        return False, cached_response

    async def cache_response(self, idempotency_key: str, response: Any) -> None:
        """Cache the result of a request under its idempotency key.

        Args:
            idempotency_key: The idempotency key.
            response: The response to cache.
        """
        cache_key = f"idempotency:{idempotency_key}"
        # Retrieve existing fingerprint
        stored_data = await self.redis.get(cache_key)
        try:
            stored_obj = json.loads(
                stored_data.decode("utf-8")
                if isinstance(stored_data, bytes)
                else stored_data
            )
        except Exception:
            logger.exception(
                "Failed to retrieve fingerprint for caching response under key %s",
                idempotency_key,
            )
            return

        # Update with response
        stored_obj["status"] = "completed"
        stored_obj["response"] = response

        updated_data = json.dumps(stored_obj, ensure_ascii=False)
        await self.redis.setex(cache_key, self.ttl_seconds, updated_data)

    # @staticmethod
    # def compute_backoff_delay(
    #     attempt: int, base_delay: float = 1.0, max_delay: float = 60.0
    # ) -> float:
    #     """Compute jittered exponential backoff delay.

    #     Formula: delay = min(base_delay * (2 ^ attempt) + random_jitter, max_delay)

    #     Args:
    #         attempt: The attempt number (0-indexed).
    #         base_delay: Base delay in seconds (default: 1.0).
    #         max_delay: Maximum delay cap in seconds (default: 60.0).

    #     Returns:
    #         Delay in seconds (float).
    #     """
    # exponential = base_delay * (2**attempt)
    # jitter = random.uniform(0, exponential)
    # return min(exponential + jitter, max_delay)

    # Execution / Data Flow
    # jitter is added to exponential before applying min(..., max_delay).
    # For initial attempts, the delay can reach up to 2x the base exponential value,
    # skewing standard exponential backoff curves.
    @staticmethod
    def compute_backoff_delay(
        attempt: int, base_delay: float = 1.0, max_delay: float = 60.0
    ) -> float:
        """Compute standard standard full jittered backoff delay.

        Formula: delay = min(base_delay * (2 ^ attempt), max_delay)

        Args:
            attempt: The attempt number (0-indexed).
            base_delay: Base delay in seconds (default: 1.0).
            max_delay: Maximum delay cap in seconds (default: 60.0).

        Returns:
            Delay in seconds (float).
        """
        calculated_delay = base_delay * (2**attempt)
        standard_full_jitter_delay = random.uniform(0, min(max_delay, calculated_delay))
        return standard_full_jitter_delay

    async def process_with_retry(
        self,
        handler: Callable[[Dict[str, Any]], Any],
        payload: Dict[str, Any],
        idempotency_key: Optional[str] = None,
        max_retries: int = 5,
        base_delay: float = 1.0,
    ) -> Any:
        """Process a message with jittered exponential backoff on transient failures.

        Args:
            handler: Async callable that processes the payload.
            payload: Message payload to process.
            idempotency_key: Optional idempotency key; if provided, enables deduplication.
            max_retries: Maximum number of retry attempts (default: 5).
            base_delay: Base delay for backoff (default: 1.0s).

        Returns:
            Result from handler.

        Raises:
            PayloadMismatchError: If idempotency key reuse is detected.
            exceptions from handler on non-transient failures or max retries exceeded.
        """
        # Check idempotency first
        if idempotency_key:
            is_new, cached = await self.check_idempotency(idempotency_key, payload)
            if not is_new:
                if cached is not None:
                    logger.info(
                        "Returning cached response for idempotency key %s",
                        idempotency_key,
                    )
                    return cached
                # Request still pending; wait a bit and retry
                logger.info(
                    "Request with key %s is still pending; retrying", idempotency_key
                )

        # Attempt processing with backoff
        last_exc = None
        for attempt in range(max_retries):
            try:
                result = await handler(payload)
                if idempotency_key:
                    await self.cache_response(idempotency_key, result)
                return result
            except asyncio.TimeoutError as exc:
                last_exc = exc
                logger.warning(
                    "Timeout on attempt %d for key %s",
                    attempt,
                    idempotency_key or "N/A",
                )
                if attempt < max_retries - 1:
                    delay = self.compute_backoff_delay(attempt, base_delay)
                    logger.debug("Backoff delay: %.2fs", delay)
                    await asyncio.sleep(delay)
            except Exception as exc:
                # Check if transient (5xx, 429)
                http_status = getattr(exc, "status_code", None)
                if http_status in (429, 500, 502, 503, 504):
                    last_exc = exc
                    logger.warning(
                        "Transient error %d on attempt %d", http_status, attempt
                    )
                    if attempt < max_retries - 1:
                        delay = self.compute_backoff_delay(attempt, base_delay)
                        await asyncio.sleep(delay)
                else:
                    # Non-transient; raise immediately
                    logger.exception("Non-transient error on attempt %d", attempt)
                    raise

        # Max retries exceeded
        logger.error("Max retries exceeded for key %s", idempotency_key or "N/A")
        raise last_exc or RuntimeError("Processing failed after max retries")


__all__ = ["AsyncMessageBus", "IdempotencyError", "PayloadMismatchError"]
