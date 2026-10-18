# routes/admin_analytics.py

from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy import select, func, Integer
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from config.limiter import limiter

from models.llm_response_tracker_db import LLMUsageLogDB

router = APIRouter(prefix="/api/v1/admin", tags=["Admin Analytics"])


@router.get("/llm-analytics")
@limiter.limit("10/minute")  # Moderate limit for heavy DB aggregate queries
async def get_llm_analytics(
    tenant_id: Optional[int] = Query(
        None, description="Optional tenant/user ID filter"
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns total request counts, aggregate token metrics, cost estimations,
    average execution latencies, and cache hit ratios grouped by provider.
    """
    # 1. Build Aggregate Query
    stmt = select(
        LLMUsageLogDB.provider,
        func.count(LLMUsageLogDB.id).label("total_requests"),
        func.sum(LLMUsageLogDB.prompt_tokens).label("prompt_tokens"),
        func.sum(LLMUsageLogDB.completion_tokens).label("completion_tokens"),
        func.sum(LLMUsageLogDB.total_tokens).label("total_tokens"),
        func.sum(LLMUsageLogDB.estimated_cost_usd).label("total_cost_usd"),
        func.avg(LLMUsageLogDB.execution_time_ms).label("avg_latency_ms"),
        func.sum(func.cast(LLMUsageLogDB.cache_hit, Integer)).label("cache_hits"),
    ).group_by(LLMUsageLogDB.provider)

    # 2. Scope Query to Specific Tenant if Provided
    if tenant_id is not None:
        stmt = stmt.where(LLMUsageLogDB.user_id == tenant_id)

    result = await db.execute(stmt)
    rows = result.all()

    if not rows:
        return {
            "tenant_id": tenant_id or "all_tenants",
            "summary": [],
            "message": "No LLM logs found matching criteria.",
        }

    # 3. Format Response Payload
    analytics_summary = []
    for row in rows:
        total_requests = row.total_requests or 1
        cache_hits = row.cache_hits or 0

        analytics_summary.append(
            {
                "provider": row.provider,
                "total_requests": row.total_requests,
                "token_breakdown": {
                    "prompt_tokens": row.prompt_tokens or 0,
                    "completion_tokens": row.completion_tokens or 0,
                    "total_tokens": row.total_tokens or 0,
                },
                "total_cost_usd": round(row.total_cost_usd or 0.0, 4),
                "avg_latency_ms": round(row.avg_latency_ms or 0.0, 2),
                "cache_hit_rate": f"{round((cache_hits / total_requests) * 100, 2)}%",
            }
        )

    return {
        "tenant_id": tenant_id or "all_tenants",
        "summary": analytics_summary,
    }
