"""Integration tests for engine.py (CalendarFunctionEngine, LLM provider fallback, and timezone context)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo
import httpx
import pytest

from engine import (
    CalendarFunctionEngine,
    OpenRouterProvider,
    GoogleProvider,
    LocalLlamaProvider,
)
from schemas import ScheduleCalendarEventFunction


@pytest.mark.integration
class TestCalendarFunctionEngineIntegration:
    @pytest.fixture
    def engine(self):
        return CalendarFunctionEngine(default_tz="UTC")

    @pytest.fixture
    def sample_schedule(self):
        from schemas import CalendarEventDateTime

        return ScheduleCalendarEventFunction(
            summary="Strategic Planning Sync",
            start=CalendarEventDateTime(
                date_time="2026-10-20T10:00:00Z", time_zone="UTC"
            ),
            end=CalendarEventDateTime(
                date_time="2026-10-20T11:00:00Z", time_zone="UTC"
            ),
            attendees=["alice@example.com", "bob@example.com"],
            description="Discuss Q4 roadmaps and deliverables.",
            location="Google Meet",
        )

    def test_timezone_context_resolution_and_dst_awareness(self, engine):
        """Verify wall-clock context resolution accurately detects UTC offset and DST status."""
        # Standard Time anchor (Winter)
        winter_anchor = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        ny_ctx_winter = engine._parse_and_format_timezone_context(
            "America/New_York", winter_anchor
        )
        assert ny_ctx_winter["iana_identifier"] == "America/New_York"
        assert ny_ctx_winter["utc_offset"] == "-05:00"
        assert ny_ctx_winter["is_dst"] == "False"

        # Daylight Saving Time anchor (Summer)
        summer_anchor = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
        ny_ctx_summer = engine._parse_and_format_timezone_context(
            "America/New_York", summer_anchor
        )
        assert ny_ctx_summer["iana_identifier"] == "America/New_York"
        assert ny_ctx_summer["utc_offset"] == "-04:00"
        assert ny_ctx_summer["is_dst"] == "True"

        # Fallback to default timezone on invalid input
        invalid_ctx = engine._parse_and_format_timezone_context(
            "Invalid/NonExistent_Zone"
        )
        assert invalid_ctx["iana_identifier"] == "UTC"
        assert invalid_ctx["utc_offset"] == "+00:00"

    def test_prompt_construction_embeds_temporal_anchors(self, engine):
        """Verify prompt embeds deterministic date/time anchors to prevent LLM hallucinations."""
        prompt = engine._build_prompt("Schedule a meeting next Tuesday at 3pm")
        assert "CURRENT DATETIME REFERENCE (UTC):" in prompt
        assert "TIMEZONE CONTEXT: iana=UTC" in prompt
        assert "User Request: Schedule a meeting next Tuesday at 3pm" in prompt

    def test_provider_fallback_sequence_openrouter_success(
        self, engine, sample_schedule
    ):
        """Verify OpenRouter is called first and returns parsed result on success."""
        with patch.object(
            engine.providers[0], "generate_schedule", return_value=sample_schedule
        ) as mock_openrouter, patch.object(
            engine.providers[1], "generate_schedule"
        ) as mock_google:
            result = engine.extract_calendar_function(
                "Schedule sprint sync tomorrow at 10am"
            )

            assert result == sample_schedule
            assert mock_openrouter.call_count == 1
            assert mock_google.call_count == 0

    def test_provider_fallback_sequence_openrouter_fails_google_succeeds(
        self, engine, sample_schedule
    ):
        """Verify that when OpenRouter fails (e.g., HTTP 500), execution falls back to GoogleProvider."""
        req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        resp = httpx.Response(500, request=req)
        openrouter_error = httpx.HTTPStatusError(
            "OpenRouter 500 Error", request=req, response=resp
        )

        with patch.object(
            engine.providers[0], "generate_schedule", side_effect=openrouter_error
        ) as mock_openrouter, patch.object(
            engine.providers[1], "generate_schedule", return_value=sample_schedule
        ) as mock_google:
            result = engine.extract_calendar_function("Schedule 1:1 sync with Alice")

            assert result == sample_schedule
            assert mock_openrouter.call_count == 1
            assert mock_google.call_count == 1

    def test_provider_fallback_all_remote_fail_localllama_succeeds(
        self, engine, sample_schedule
    ):
        """Verify that when both OpenRouter and Google fail, LocalLlamaProvider handles extraction."""
        with patch.object(
            engine.providers[0],
            "generate_schedule",
            side_effect=RuntimeError("OpenRouter down"),
        ), patch.object(
            engine.providers[1],
            "generate_schedule",
            side_effect=RuntimeError("Google quota exceeded"),
        ), patch.object(
            engine.providers[2], "generate_schedule", return_value=sample_schedule
        ) as mock_local:
            result = engine.extract_calendar_function(
                "Emergency incident retrospective"
            )

            assert result == sample_schedule
            assert mock_local.call_count == 1

    def test_circuit_breaker_tripping_on_rate_limits(self, engine, sample_schedule):
        """Verify HTTP 422/429 triggers circuit breaker flag and skips tripped provider."""
        req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        resp = httpx.Response(429, request=req)
        rate_limit_error = httpx.HTTPStatusError(
            "Rate limited", request=req, response=resp
        )

        with patch.object(
            engine.providers[0], "generate_schedule", side_effect=rate_limit_error
        ), patch("engine.is_tripped", side_effect=[False, False, False]), patch(
            "engine.trip"
        ) as mock_trip, patch.object(
            engine.providers[1], "generate_schedule", return_value=sample_schedule
        ):
            result = engine.extract_calendar_function("Book quarterly review")

            assert result == sample_schedule
            mock_trip.assert_called_once_with("OpenRouterProvider", ttl_seconds=60)
