# services/usage_tracker.py

import logging
from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from models.llm_response_tracker_db import LLMUsageLogDB

logger = logging.getLogger(__name__)

# Standard pricing benchmarks per 1M tokens for Gemini 2.5 Flash Lite
PRICING_TABLE = {
    "gemini-2.5-flash-lite": {
        "prompt": 0.075 / 1_000_000,
        "cached_prompt": 0.01875 / 1_000_000,  # ~75% savings
        "completion": 0.30 / 1_000_000,
    },
    "google/gemini-2.5-flash-lite": {
        "prompt": 0.075 / 1_000_000,
        "cached_prompt": 0.01875 / 1_000_000,
        "completion": 0.30 / 1_000_000,
    },
}


class UsageTrackerService:
    @staticmethod
    def calculate_cost(
        model_name: str,
        prompt_tokens: int,
        cached_tokens: int,
        completion_tokens: int,
    ) -> float:
        rates = PRICING_TABLE.get(model_name, PRICING_TABLE["gemini-2.5-flash-lite"])

        uncached_prompt = max(0, prompt_tokens - cached_tokens)
        cost = (
            (uncached_prompt * rates["prompt"])
            + (cached_tokens * rates["cached_prompt"])
            + (completion_tokens * rates["completion"])
        )
        return round(cost, 8)

    @classmethod
    async def log_usage(
        cls,
        session: AsyncSession,
        provider: str,
        model_name: str,
        query_text: str,
        system_instruction: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
        execution_time_ms: float = 0.0,
        user_id: Optional[int] = None,
        raw_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Logs LLM metrics using the caller-provided session without altering its lifecycle."""

        try:
            total_tokens = prompt_tokens + completion_tokens
            cache_hit = cached_tokens > 0
            cost = cls.calculate_cost(
                model_name, prompt_tokens, cached_tokens, completion_tokens
            )

            async with session:
                log_entry = LLMUsageLogDB(
                    user_id=user_id,
                    provider=provider,
                    model_name=model_name,
                    query_text=query_text,
                    system_instruction=system_instruction,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    cached_tokens=cached_tokens,
                    cache_hit=cache_hit,
                    estimated_cost_usd=cost,
                    execution_time_ms=execution_time_ms,
                    raw_response_meta=raw_meta or {},
                )
                session.add(log_entry)
                await session.commit()
        except Exception as e:
            logger.error(f"Failed to log LLM usage analytics: {str(e)}")
            await session.rollback()
            return None
