"""
Unit tests for services/usage_tracker/service.py
"""

from unittest.mock import AsyncMock, MagicMock
import pytest

from models.llm_response_tracker_db import LLMUsageLogDB
from services.usage_tracker.service import PRICING_TABLE, UsageTrackerService


@pytest.mark.unit
class TestUsageTrackerService:
    @pytest.fixture
    def mock_session(self):
        """Provide a mock AsyncSession that supports async context manager protocol."""
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.__aexit__.return_value = None
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        session.add = MagicMock()
        return session

    # -------------------------------------------------------------------------
    # 1. Cost Calculation Logic
    # -------------------------------------------------------------------------

    def test_calculate_cost_standard_model_without_cache(self):
        """Verify cost calculation for standard Gemini model without prompt caching."""
        # Rates for gemini-2.5-flash-lite: prompt = $0.075/1M, completion = $0.30/1M
        # 1,000,000 prompt tokens -> $0.075
        # 1,000,000 completion tokens -> $0.30
        cost = UsageTrackerService.calculate_cost(
            model_name="gemini-2.5-flash-lite",
            prompt_tokens=1_000_000,
            cached_tokens=0,
            completion_tokens=1_000_000,
        )
        assert cost == 0.375

    def test_calculate_cost_with_cached_prompt_savings(self):
        """Verify cached prompt tokens receive discount rate (~75% reduction)."""
        # Prompt: 10,000 total with 4,000 cached (6,000 uncached)
        # Uncached prompt: 6,000 * (0.075 / 1,000,000) = 0.00045
        # Cached prompt: 4,000 * (0.01875 / 1,000,000) = 0.000075
        # Completion: 5,000 * (0.30 / 1,000,000) = 0.0015
        # Total: 0.00045 + 0.000075 + 0.0015 = 0.002025
        cost = UsageTrackerService.calculate_cost(
            model_name="gemini-2.5-flash-lite",
            prompt_tokens=10_000,
            cached_tokens=4_000,
            completion_tokens=5_000,
        )
        assert cost == 0.002025

    def test_calculate_cost_model_alias(self):
        """Verify model alias 'google/gemini-2.5-flash-lite' resolves matching rates."""
        cost = UsageTrackerService.calculate_cost(
            model_name="google/gemini-2.5-flash-lite",
            prompt_tokens=200_000,
            cached_tokens=0,
            completion_tokens=100_000,
        )
        # 200,000 * 0.075/1M = 0.015
        # 100,000 * 0.30/1M = 0.03
        # Total = 0.045
        assert cost == 0.045

    def test_calculate_cost_unknown_model_fallback(self):
        """Verify unmapped model names fall back to default benchmark pricing."""
        unknown_cost = UsageTrackerService.calculate_cost(
            model_name="unregistered/proprietary-llm-v1",
            prompt_tokens=50_000,
            cached_tokens=10_000,
            completion_tokens=25_000,
        )
        expected_cost = UsageTrackerService.calculate_cost(
            model_name="gemini-2.5-flash-lite",
            prompt_tokens=50_000,
            cached_tokens=10_000,
            completion_tokens=25_000,
        )
        assert unknown_cost == expected_cost

    def test_calculate_cost_zero_tokens(self):
        """Verify cost calculation returns 0.0 when token counts are zero."""
        cost = UsageTrackerService.calculate_cost(
            model_name="gemini-2.5-flash-lite",
            prompt_tokens=0,
            cached_tokens=0,
            completion_tokens=0,
        )
        assert cost == 0.0

    def test_calculate_cost_cached_tokens_exceeding_prompt_tokens(self):
        """Verify uncached prompt tokens clamp to zero when cached tokens exceed prompt count."""
        # 100 prompt tokens, 200 cached tokens -> uncached = max(0, 100 - 200) = 0
        # 200 cached tokens * 0.01875 / 1,000,000 = 0.00000375
        cost = UsageTrackerService.calculate_cost(
            model_name="gemini-2.5-flash-lite",
            prompt_tokens=100,
            cached_tokens=200,
            completion_tokens=0,
        )
        assert cost == 0.00000375

    # -------------------------------------------------------------------------
    # 2. Asynchronous Database Logging & Exception Rollback
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_log_usage_success_commits_db_entry(self, mock_session):
        """Verify asynchronous usage logging creates LLMUsageLogDB record and commits."""
        await UsageTrackerService.log_usage(
            session=mock_session,
            provider="google",
            model_name="gemini-2.5-flash-lite",
            query_text="Schedule sprint planning on Friday",
            system_instruction="Extract calendar meeting details",
            prompt_tokens=500,
            completion_tokens=200,
            cached_tokens=150,
            execution_time_ms=340.5,
            user_id=88,
            raw_meta={"finish_reason": "STOP", "candidates_token_count": 200},
        )

        mock_session.add.assert_called_once()
        log_entry = mock_session.add.call_args[0][0]

        assert isinstance(log_entry, LLMUsageLogDB)
        assert log_entry.user_id == 88
        assert log_entry.provider == "google"
        assert log_entry.model_name == "gemini-2.5-flash-lite"
        assert log_entry.query_text == "Schedule sprint planning on Friday"
        assert log_entry.system_instruction == "Extract calendar meeting details"
        assert log_entry.prompt_tokens == 500
        assert log_entry.completion_tokens == 200
        assert log_entry.total_tokens == 700  # 500 + 200
        assert log_entry.cached_tokens == 150
        assert log_entry.cache_hit is True
        assert log_entry.execution_time_ms == 340.5
        assert log_entry.raw_response_meta == {
            "finish_reason": "STOP",
            "candidates_token_count": 200,
        }
        assert log_entry.estimated_cost_usd > 0.0

        mock_session.commit.assert_awaited_once()
        mock_session.rollback.assert_not_called()

    @pytest.mark.anyio
    async def test_log_usage_cache_hit_false_when_no_cached_tokens(self, mock_session):
        """Verify cache_hit flag is False when cached_tokens is zero."""
        await UsageTrackerService.log_usage(
            session=mock_session,
            provider="openrouter",
            model_name="gemini-2.5-flash-lite",
            query_text="Hello LLM",
            system_instruction="Be helpful",
            prompt_tokens=50,
            completion_tokens=25,
            cached_tokens=0,
        )

        mock_session.add.assert_called_once()
        log_entry = mock_session.add.call_args[0][0]
        assert log_entry.cache_hit is False
        assert log_entry.cached_tokens == 0
        mock_session.commit.assert_awaited_once()

    @pytest.mark.anyio
    async def test_log_usage_db_failure_triggers_rollback(self, mock_session):
        """Verify database exception during commit initiates rollback and returns gracefully."""
        mock_session.commit.side_effect = RuntimeError(
            "Connection to PostgreSQL closed unexpectedly"
        )

        result = await UsageTrackerService.log_usage(
            session=mock_session,
            provider="google",
            model_name="gemini-2.5-flash-lite",
            query_text="Test query",
            system_instruction="Test system",
            prompt_tokens=100,
            completion_tokens=50,
        )

        assert result is None
        mock_session.rollback.assert_awaited_once()

    @pytest.mark.anyio
    async def test_log_usage_defaults_for_optional_arguments(self, mock_session):
        """Verify default values for user_id, raw_meta, cached_tokens, and execution_time_ms."""
        await UsageTrackerService.log_usage(
            session=mock_session,
            provider="local",
            model_name="gemini-2.5-flash-lite",
            query_text="Quick check",
            system_instruction="Prompt",
            prompt_tokens=10,
            completion_tokens=5,
        )

        log_entry = mock_session.add.call_args[0][0]
        assert log_entry.user_id is None
        assert log_entry.raw_response_meta == {}
        assert log_entry.cached_tokens == 0
        assert log_entry.execution_time_ms == 0.0
        assert log_entry.cache_hit is False
        mock_session.commit.assert_awaited_once()
