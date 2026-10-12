"""
Unit tests for routes/admin_analytics.py
"""

from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.database import get_db
from config.limiter import limiter
from routes.admin_analytics import router


@pytest.mark.unit
class TestAdminAnalytics:
    @pytest.fixture
    def mock_db_session(self):
        """Provide a mock AsyncSession."""
        session = AsyncMock()
        return session

    @pytest.fixture
    def client_and_db(self, mock_db_session):
        """Build an isolated FastAPI test application with router and db dependency override."""
        app = FastAPI()
        app.state.limiter = limiter
        app.include_router(router)

        async def override_get_db():
            yield mock_db_session

        app.dependency_overrides[get_db] = override_get_db
        client = TestClient(app)
        yield client, mock_db_session
        app.dependency_overrides.clear()

    def test_get_llm_analytics_happy_path(self, client_and_db):
        """Verify successful calculation of token, cost, latency, and cache hit metrics across providers."""
        client, mock_db = client_and_db

        row_openai = MagicMock(
            provider="openai",
            total_requests=100,
            prompt_tokens=5000,
            completion_tokens=2500,
            total_tokens=7500,
            total_cost_usd=0.15234,
            avg_latency_ms=350.256,
            cache_hits=40,
        )
        row_anthropic = MagicMock(
            provider="anthropic",
            total_requests=50,
            prompt_tokens=3000,
            completion_tokens=1500,
            total_tokens=4500,
            total_cost_usd=0.08219,
            avg_latency_ms=420.12,
            cache_hits=10,
        )

        mock_result = MagicMock()
        mock_result.all.return_value = [row_openai, row_anthropic]
        mock_db.execute.return_value = mock_result

        response = client.get("/api/v1/admin/llm-analytics")
        assert response.status_code == 200

        data = response.json()
        assert data["tenant_id"] == "all_tenants"
        assert len(data["summary"]) == 2

        summary_openai = data["summary"][0]
        assert summary_openai["provider"] == "openai"
        assert summary_openai["total_requests"] == 100
        assert summary_openai["token_breakdown"]["prompt_tokens"] == 5000
        assert summary_openai["token_breakdown"]["completion_tokens"] == 2500
        assert summary_openai["token_breakdown"]["total_tokens"] == 7500
        assert summary_openai["total_cost_usd"] == 0.1523
        assert summary_openai["avg_latency_ms"] == 350.26
        assert summary_openai["cache_hit_rate"] == "40.0%"

        summary_anthropic = data["summary"][1]
        assert summary_anthropic["provider"] == "anthropic"
        assert summary_anthropic["cache_hit_rate"] == "20.0%"

    def test_get_llm_analytics_with_tenant_id_filter(self, client_and_db):
        """Verify tenant-scoped query execution and response formatting."""
        client, mock_db = client_and_db

        row_gemini = MagicMock(
            provider="google",
            total_requests=25,
            prompt_tokens=1000,
            completion_tokens=500,
            total_tokens=1500,
            total_cost_usd=0.02,
            avg_latency_ms=180.5,
            cache_hits=5,
        )
        mock_result = MagicMock()
        mock_result.all.return_value = [row_gemini]
        mock_db.execute.return_value = mock_result

        response = client.get("/api/v1/admin/llm-analytics?tenant_id=42")
        assert response.status_code == 200

        data = response.json()
        assert data["tenant_id"] == 42
        assert len(data["summary"]) == 1
        assert data["summary"][0]["provider"] == "google"
        assert mock_db.execute.called

    def test_get_llm_analytics_empty_result(self, client_and_db):
        """Verify behavior when no LLM usage records match the filter."""
        client, mock_db = client_and_db

        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_db.execute.return_value = mock_result

        response = client.get("/api/v1/admin/llm-analytics?tenant_id=999")
        assert response.status_code == 200

        data = response.json()
        assert data["tenant_id"] == 999
        assert data["summary"] == []
        assert data["message"] == "No LLM logs found matching criteria."

    def test_get_llm_analytics_none_aggregates_fallback(self, client_and_db):
        """Verify resilient handling of None/null database aggregations without division by zero."""
        client, mock_db = client_and_db

        row_nulls = MagicMock(
            provider="local_llm",
            total_requests=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            total_cost_usd=None,
            avg_latency_ms=None,
            cache_hits=None,
        )
        mock_result = MagicMock()
        mock_result.all.return_value = [row_nulls]
        mock_db.execute.return_value = mock_result

        response = client.get("/api/v1/admin/llm-analytics")
        assert response.status_code == 200

        data = response.json()
        summary = data["summary"][0]
        assert summary["provider"] == "local_llm"
        assert summary["token_breakdown"]["prompt_tokens"] == 0
        assert summary["total_cost_usd"] == 0.0
        assert summary["avg_latency_ms"] == 0.0
        assert summary["cache_hit_rate"] == "0.0%"

    def test_get_llm_analytics_invalid_tenant_id_type(self, client_and_db):
        """Verify validation error when tenant_id query parameter is not an integer."""
        client, _ = client_and_db

        response = client.get("/api/v1/admin/llm-analytics?tenant_id=invalid_tenant")
        assert response.status_code == 422
