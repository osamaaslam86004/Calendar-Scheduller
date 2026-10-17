"""Integration tests for services/usage_tracker/service.py."""

from unittest.mock import AsyncMock, MagicMock
import pytest

from models.llm_response_tracker_db import LLMUsageLogDB
from services.usage_tracker.service import UsageTrackerService, PRICING_TABLE


@pytest.mark.integration
@pytest.mark.anyio
class TestUsageTrackerServiceIntegration:
    def test_calculate_cost_with_pricing_table(self):
        """Verify calculate_cost handles uncached prompt, cached prompt savings, and completion tokens."""
        rates = PRICING_TABLE["gemini-2.5-flash-lite"]

        # 10,000 prompt tokens (with 2,000 cached) and 1,000 completion tokens
        cost = UsageTrackerService.calculate_cost(
            model_name="gemini-2.5-flash-lite",
            prompt_tokens=10_000,
            cached_tokens=2_000,
            completion_tokens=1_000,
        )

        expected = (
            (8_000 * rates["prompt"])
            + (2_000 * rates["cached_prompt"])
            + (1_000 * rates["completion"])
        )
        assert cost == round(expected, 8)

    def test_calculate_cost_fallback_for_unknown_model(self):
        """Verify unknown models fallback safely to default flash-lite pricing."""
        cost = UsageTrackerService.calculate_cost(
            model_name="unknown-custom-model-v1",
            prompt_tokens=1_000_000,
            cached_tokens=0,
            completion_tokens=0,
        )
        assert cost == pytest.approx(0.075, rel=1e-5)

    async def test_log_usage_persists_entry_and_commits(self):
        """Verify log_usage creates LLMUsageLogDB record and commits via session."""
        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None

        await UsageTrackerService.log_usage(
            session=mock_session,
            provider="google",
            model_name="gemini-2.5-flash-lite",
            query_text="Schedule meeting with Alice tomorrow at 3pm",
            system_instruction="Extract meeting details",
            prompt_tokens=500,
            completion_tokens=150,
            cached_tokens=100,
            execution_time_ms=234.5,
            user_id=10,
            raw_meta={"finish_reason": "STOP"},
        )

        mock_session.add.assert_called_once()
        added_entry = mock_session.add.call_args[0][0]
        assert isinstance(added_entry, LLMUsageLogDB)
        assert added_entry.user_id == 10
        assert added_entry.provider == "google"
        assert added_entry.prompt_tokens == 500
        assert added_entry.completion_tokens == 150
        assert added_entry.total_tokens == 650
        assert added_entry.cache_hit is True
        assert added_entry.estimated_cost_usd > 0
        mock_session.commit.assert_awaited_once()

    async def test_log_usage_rolls_back_on_db_exception(self):
        """Verify database errors trigger rollback without propagating exception to caller."""
        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None
        mock_session.commit.side_effect = RuntimeError("Database connection reset")

        # Must not raise
        await UsageTrackerService.log_usage(
            session=mock_session,
            provider="openrouter",
            model_name="gemini-2.5-flash-lite",
            query_text="Sync",
            system_instruction="Inst",
            prompt_tokens=100,
            completion_tokens=50,
        )

        mock_session.rollback.assert_awaited_once()
