"""End-to-End tests for administrative LLM analytics workflows."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.llm_response_tracker_db import LLMUsageLogDB


@pytest.mark.asyncio
async def test_admin_llm_analytics_empty(async_client: AsyncClient):
    """Test LLM analytics endpoint with no logs returns empty summary."""
    response = await async_client.get("/api/v1/admin/llm-analytics")

    assert response.status_code == 200
    data = response.json()
    assert data["tenant_id"] == "all_tenants"
    assert data["summary"] == []
    assert "No LLM logs found" in data["message"]


@pytest.mark.asyncio
async def test_admin_llm_analytics_populated(
    async_client: AsyncClient, db_session: AsyncSession
):
    """Test LLM analytics aggregates token metrics, cost, latency, and cache."""
    log1 = LLMUsageLogDB(
        user_id=1,
        provider="google",
        model_name="gemma-2-2b",
        query_text="Schedule meeting with design team tomorrow",
        prompt_tokens=150,
        completion_tokens=50,
        total_tokens=200,
        cached_tokens=0,
        cache_hit=False,
        estimated_cost_usd=0.0025,
        execution_time_ms=350.0,
    )
    log2 = LLMUsageLogDB(
        user_id=1,
        provider="google",
        model_name="gemma-2-2b",
        query_text="Schedule follow-up call with engineering",
        prompt_tokens=100,
        completion_tokens=40,
        total_tokens=140,
        cached_tokens=100,
        cache_hit=True,
        estimated_cost_usd=0.0010,
        execution_time_ms=120.0,
    )
    log3 = LLMUsageLogDB(
        user_id=2,
        provider="openrouter",
        model_name="anthropic/claude-3-haiku",
        query_text="Parse calendar invite request",
        prompt_tokens=200,
        completion_tokens=80,
        total_tokens=280,
        cached_tokens=0,
        cache_hit=False,
        estimated_cost_usd=0.0050,
        execution_time_ms=450.0,
    )

    db_session.add_all([log1, log2, log3])
    await db_session.commit()

    response = await async_client.get("/api/v1/admin/llm-analytics")

    assert response.status_code == 200
    data = response.json()
    assert data["tenant_id"] == "all_tenants"

    summary = data["summary"]
    assert len(summary) == 2

    google_entry = next(
        (s for s in summary if s["provider"] == "google"), None
    )
    assert google_entry is not None
    assert google_entry["total_requests"] == 2
    assert google_entry["token_breakdown"]["total_tokens"] == 340
    assert google_entry["cache_hit_rate"] == "50.0%"

    openrouter_entry = next(
        (s for s in summary if s["provider"] == "openrouter"), None
    )
    assert openrouter_entry is not None
    assert openrouter_entry["total_requests"] == 1
    assert openrouter_entry["cache_hit_rate"] == "0.0%"


@pytest.mark.asyncio
async def test_admin_llm_analytics_filtered_by_tenant(
    async_client: AsyncClient, db_session: AsyncSession
):
    """Test LLM analytics endpoint properly filters metrics by tenant_id."""
    log_user1 = LLMUsageLogDB(
        user_id=101,
        provider="google",
        model_name="gemma-2-2b",
        query_text="Tenant 101 schedule request",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cache_hit=False,
        estimated_cost_usd=0.002,
        execution_time_ms=200.0,
    )
    log_user2 = LLMUsageLogDB(
        user_id=202,
        provider="google",
        model_name="gemma-2-2b",
        query_text="Tenant 202 schedule request",
        prompt_tokens=500,
        completion_tokens=200,
        total_tokens=700,
        cache_hit=True,
        estimated_cost_usd=0.010,
        execution_time_ms=180.0,
    )

    db_session.add_all([log_user1, log_user2])
    await db_session.commit()

    response = await async_client.get(
        "/api/v1/admin/llm-analytics", params={"tenant_id": 101}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["tenant_id"] == "101"

    summary = data["summary"]
    assert len(summary) == 1
    assert summary[0]["provider"] == "google"
    assert summary[0]["total_requests"] == 1
    assert summary[0]["token_breakdown"]["total_tokens"] == 150
