"""Lightweight performance regression tests for the meeting scheduler.

These tests intentionally avoid live Redis, PostgreSQL, Celery, Google Calendar,
and LLM calls. They measure the local request/schema and idempotency overhead so
that results are repeatable in CI. External-service latency needs a separate
staging/load test against a deployed service.

Optional environment overrides:
    SCHEDULER_PERF_SCHEMA_SAMPLES (default: 500)
    SCHEDULER_PERF_MAX_REQUEST_P95_MS (default: 10)
    SCHEDULER_PERF_MAX_EVENT_P95_MS (default: 15)
    SCHEDULER_PERF_IDEMPOTENCY_SAMPLES (default: 500)
    SCHEDULER_PERF_MAX_IDEMPOTENCY_P95_MS (default: 10)
    SCHEDULER_PERF_CONCURRENT_REQUESTS (default: 500)
    SCHEDULER_PERF_MAX_CONCURRENT_SECONDS (default: 3)
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from typing import Any

import pytest

from messaging.bus import AsyncMessageBus
from schemas import ScheduleCalendarEventFunction, UserScheduleRequest

pytestmark = pytest.mark.performance


class InMemoryRedis:
    """Tiny async Redis double; no Redis server is needed for these tests."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._values.get(key)

    async def setex(self, key: str, _ttl_seconds: int, value: str) -> bool:
        self._values[key] = value
        return True


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError as exc:
        raise AssertionError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise AssertionError(f"{name} must be a number") from exc
    if value <= 0:
        raise AssertionError(f"{name} must be greater than zero")
    return value


def _p95_ms(samples_seconds: list[float]) -> float:
    """Return nearest-rank p95 latency in milliseconds."""
    assert samples_seconds, "At least one latency sample is required"
    ordered = sorted(samples_seconds)
    index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index] * 1000.0


def _schedule_request(index: int) -> dict[str, str]:
    return {
        "request_text": (
            f"Schedule the project sync {index} with alex@example.com "
            "tomorrow at 3 PM UTC for 30 minutes."
        )
    }


def _calendar_event(index: int) -> dict[str, Any]:
    return {
        "summary": f"Project sync {index}",
        "description": "Discuss project risks, milestones, and action items.",
        "location": "Online",
        "start": {
            "date_time": "2026-10-20T15:00:00Z",
            "time_zone": "UTC",
        },
        "end": {
            "date_time": "2026-10-20T15:30:00Z",
            "time_zone": "UTC",
        },
        "attendees": ["alex@example.com", "sam@example.com"],
        "send_updates": "all",
    }


def test_user_schedule_request_validation_p95() -> None:
    """Request payload validation should remain fast at the API boundary."""
    samples = _env_int("SCHEDULER_PERF_SCHEMA_SAMPLES", 500)
    latencies: list[float] = []

    for index in range(samples):
        payload = _schedule_request(index)
        start = time.perf_counter()
        parsed = UserScheduleRequest.model_validate(payload)
        latencies.append(time.perf_counter() - start)
        assert parsed.request_text == payload["request_text"]

    p95_ms = _p95_ms(latencies)
    limit_ms = _env_float("SCHEDULER_PERF_MAX_REQUEST_P95_MS", 10.0)
    assert p95_ms < limit_ms, (
        f"UserScheduleRequest p95 was {p95_ms:.3f} ms; target is below "
        f"{limit_ms:.3f} ms across {samples} samples"
    )


def test_calendar_event_schema_validation_p95() -> None:
    """Structured calendar event validation should stay within a small budget."""
    samples = _env_int("SCHEDULER_PERF_SCHEMA_SAMPLES", 500)
    latencies: list[float] = []

    for index in range(samples):
        payload = _calendar_event(index)
        start = time.perf_counter()
        parsed = ScheduleCalendarEventFunction.model_validate(payload)
        latencies.append(time.perf_counter() - start)
        assert len(parsed.attendees) == 2

    p95_ms = _p95_ms(latencies)
    limit_ms = _env_float("SCHEDULER_PERF_MAX_EVENT_P95_MS", 15.0)
    assert p95_ms < limit_ms, (
        f"ScheduleCalendarEventFunction p95 was {p95_ms:.3f} ms; target is "
        f"below {limit_ms:.3f} ms across {samples} samples"
    )


def test_idempotency_cache_hit_p95() -> None:
    """A repeated scheduling request should be resolved from the idempotency cache."""
    async def run_test() -> tuple[list[float], int]:
        samples = _env_int("SCHEDULER_PERF_IDEMPOTENCY_SAMPLES", 500)
        bus = AsyncMessageBus(redis_client=InMemoryRedis(), ttl_seconds=3600)
        key = "schedule_meeting:perf-user:stable-request"
        payload = {
            "user_id": 42,
            "request_text": "Schedule a 30-minute team sync tomorrow at 3 PM UTC.",
        }

        is_new, cached = await bus.check_idempotency(key, payload)
        assert is_new is True and cached is None
        await bus.cache_response(key, {"task_id": "performance-task-001", "status": "PENDING"})

        latencies: list[float] = []
        for _ in range(samples):
            start = time.perf_counter()
            is_new, cached = await bus.check_idempotency(key, payload)
            latencies.append(time.perf_counter() - start)
            assert is_new is False
            assert cached == {"task_id": "performance-task-001", "status": "PENDING"}
        return latencies, samples

    latencies, samples = asyncio.run(run_test())
    p95_ms = _p95_ms(latencies)
    limit_ms = _env_float("SCHEDULER_PERF_MAX_IDEMPOTENCY_P95_MS", 10.0)
    assert p95_ms < limit_ms, (
        f"Idempotency cache-hit p95 was {p95_ms:.3f} ms; target is below "
        f"{limit_ms:.3f} ms across {samples} samples"
    )


def test_concurrent_unique_schedule_idempotency_checks() -> None:
    """A burst of unique schedule requests should complete without lost results."""
    async def run_test() -> tuple[int, float]:
        requests = _env_int("SCHEDULER_PERF_CONCURRENT_REQUESTS", 500)
        bus = AsyncMessageBus(redis_client=InMemoryRedis(), ttl_seconds=3600)

        async def check(index: int) -> tuple[bool, Any]:
            key = f"schedule_meeting:load-test:{index}"
            payload = {
                "user_id": index + 1,
                "request_text": f"Schedule performance test meeting {index} tomorrow at 1 PM UTC.",
            }
            return await bus.check_idempotency(key, payload)

        start = time.perf_counter()
        results = await asyncio.gather(*(check(index) for index in range(requests)))
        elapsed = time.perf_counter() - start

        assert len(results) == requests
        assert all(is_new and cached is None for is_new, cached in results)
        return requests, elapsed

    request_count, elapsed_seconds = asyncio.run(run_test())
    limit_seconds = _env_float("SCHEDULER_PERF_MAX_CONCURRENT_SECONDS", 3.0)
    assert elapsed_seconds < limit_seconds, (
        f"{request_count} concurrent idempotency checks took {elapsed_seconds:.3f} s; "
        f"target is below {limit_seconds:.3f} s"
    )


def test_idempotency_fingerprint_generation_throughput() -> None:
    """Fingerprint generation should be deterministic and inexpensive."""
    samples = _env_int("SCHEDULER_PERF_SCHEMA_SAMPLES", 500)
    payloads = [
        {"user_id": index, "request_text": _schedule_request(index)["request_text"]}
        for index in range(samples)
    ]

    start = time.perf_counter()
    fingerprints = [AsyncMessageBus.compute_fingerprint(payload) for payload in payloads]
    elapsed = time.perf_counter() - start

    assert len(fingerprints) == samples
    assert len(set(fingerprints)) == samples
    assert all(len(fingerprint) == 64 for fingerprint in fingerprints)
    assert elapsed < 2.0, (
        f"Generated {samples} SHA-256 fingerprints in {elapsed:.3f} s; target is below 2 s"
    )


def test_payload_fingerprint_is_stable_for_equivalent_key_order() -> None:
    """Equivalent JSON mappings should produce the same idempotency fingerprint."""
    first = {"user_id": 7, "request_text": "Schedule a review tomorrow at noon."}
    second = {"request_text": "Schedule a review tomorrow at noon.", "user_id": 7}

    assert AsyncMessageBus.compute_fingerprint(first) == AsyncMessageBus.compute_fingerprint(second)
