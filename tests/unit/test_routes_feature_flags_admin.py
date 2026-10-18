"""
Unit tests for routes/feature_flags_admin.py
"""

from unittest.mock import MagicMock, patch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.feature_flags_admin import router
from utilities.security import build_tenant_context


@pytest.mark.unit
class TestFeatureFlagsAdmin:
    @pytest.fixture
    def app(self):
        """Build test application mounting feature_flags_admin router."""
        app = FastAPI()
        app.include_router(router)
        return app

    @pytest.fixture
    def client(self, app):
        """Provide TestClient for feature flag endpoints."""
        return TestClient(app)

    @pytest.fixture
    def admin_headers(self):
        """Provide valid tenant authentication headers with 'admin' scope."""
        token = build_tenant_context("tenant_admin_root", scopes=["admin"])
        return {"X-Tenant-JWT": token}

    @pytest.fixture
    def non_admin_headers(self):
        """Provide tenant authentication headers lacking 'admin' scope."""
        token = build_tenant_context("tenant_user_1", scopes=["user", "calendar:read"])
        return {"X-Tenant-JWT": token}

    def test_unauthorized_when_auth_headers_missing(self, client):
        """Verify endpoint rejects requests with HTTP 401 when tenant credentials are absent."""
        response = client.get("/api/v1/admin/flags/get/test_flag")
        assert response.status_code == 401
        assert response.json()["detail"] == "Tenant context required"

    def test_forbidden_when_admin_scope_is_missing(self, client, non_admin_headers):
        """Verify endpoint rejects requests with HTTP 403 when user lacks 'admin' scope."""
        response = client.get(
            "/api/v1/admin/flags/get/test_flag", headers=non_admin_headers
        )
        assert response.status_code == 403
        assert "Missing scope: admin" in response.json()["detail"]

    @patch("routes.feature_flags_admin.set_feature_flag")
    def test_set_flag_success(self, mock_set, client, admin_headers):
        """Verify dynamic feature flag creation/update."""
        mock_set.return_value = {"updated": True}

        payload = {"flag_name": "ai_scheduling_v2", "value": True, "ttl_seconds": 600}
        response = client.post(
            "/api/v1/admin/flags/set", json=payload, headers=admin_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["flag"] == "ai_scheduling_v2"
        assert data["value"] is True
        mock_set.assert_called_once_with("ai_scheduling_v2", True, 600)

    @patch("routes.feature_flags_admin.get_feature_flag")
    def test_get_flag_found(self, mock_get, client, admin_headers):
        """Verify retrieving an existing feature flag."""
        mock_get.return_value = "gpt-4o-mini"

        response = client.get(
            "/api/v1/admin/flags/get/llm_model_override", headers=admin_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["flag"] == "llm_model_override"
        assert data["value"] == "gpt-4o-mini"

    @patch("routes.feature_flags_admin.get_feature_flag")
    def test_get_flag_not_found(self, mock_get, client, admin_headers):
        """Verify retrieving an absent feature flag returns status 'not_found'."""
        mock_get.return_value = None

        response = client.get(
            "/api/v1/admin/flags/get/non_existent_flag", headers=admin_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "not_found"
        assert data["value"] is None

    @patch("routes.feature_flags_admin.get_rate_limit_capacity")
    @patch("routes.feature_flags_admin.set_rate_limit_capacity")
    def test_update_rate_limit_capacity_success(
        self, mock_set, mock_get, client, admin_headers
    ):
        """Verify updating provider rate limit capacity."""
        mock_set.return_value = {"capacity_set": 150}
        mock_get.return_value = 100

        payload = {"provider": "openai", "capacity": 150, "ttl_seconds": 300}
        response = client.post(
            "/api/v1/admin/flags/rate-limit/set", json=payload, headers=admin_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["provider"] == "openai"
        assert data["capacity"] == 150
        assert data["previous_capacity"] == 100

    def test_update_rate_limit_capacity_invalid_value(self, client, admin_headers):
        """Verify validation error when rate limit capacity is less than 1."""
        payload = {"provider": "openai", "capacity": 0}
        response = client.post(
            "/api/v1/admin/flags/rate-limit/set", json=payload, headers=admin_headers
        )
        assert response.status_code == 422

    @patch("routes.feature_flags_admin.get_circuit_breaker_threshold")
    @patch("routes.feature_flags_admin.set_circuit_breaker_threshold")
    def test_update_circuit_breaker_success(
        self, mock_set, mock_get, client, admin_headers
    ):
        """Verify updating circuit breaker threshold."""
        mock_set.return_value = {"threshold_set": 10}
        mock_get.return_value = 5

        payload = {"provider": "google_calendar", "threshold": 10}
        response = client.post(
            "/api/v1/admin/flags/circuit-breaker/set",
            json=payload,
            headers=admin_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["threshold"] == 10
        assert data["previous_threshold"] == 5

    def test_update_circuit_breaker_invalid_threshold(self, client, admin_headers):
        """Verify validation error when circuit breaker threshold is less than 1."""
        payload = {"provider": "google_calendar", "threshold": -1}
        response = client.post(
            "/api/v1/admin/flags/circuit-breaker/set",
            json=payload,
            headers=admin_headers,
        )
        assert response.status_code == 422

    @patch("routes.feature_flags_admin.get_effective_refill_rate")
    @patch("routes.feature_flags_admin.set_token_refill_rate")
    def test_update_token_refill_rate_success(
        self, mock_set, mock_get, client, admin_headers
    ):
        """Verify updating token refill rate multiplier within allowable bounds [0.1, 2.0]."""
        mock_set.return_value = {"rate_applied": 1.5}
        mock_get.return_value = 1.0

        payload = {"provider": "default", "rate": 1.5}
        response = client.post(
            "/api/v1/admin/flags/token-refill/set",
            json=payload,
            headers=admin_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["refill_rate"] == 1.5

    def test_update_token_refill_rate_out_of_bounds(self, client, admin_headers):
        """Verify validation error when token refill multiplier exceeds 2.0 or is below 0.1."""
        response_high = client.post(
            "/api/v1/admin/flags/token-refill/set",
            json={"provider": "default", "rate": 5.0},
            headers=admin_headers,
        )
        assert response_high.status_code == 422

        response_low = client.post(
            "/api/v1/admin/flags/token-refill/set",
            json={"provider": "default", "rate": 0.01},
            headers=admin_headers,
        )
        assert response_low.status_code == 422

    @patch("routes.feature_flags_admin.apply_dynamic_throttle_to_job")
    def test_throttle_background_job_success(
        self, mock_throttle, client, admin_headers
    ):
        """Verify dynamic background job throttling toggle."""
        mock_throttle.return_value = {"throttled_jobs": ["calendar_sync"]}

        payload = {"job_type": "calendar_sync", "throttled": True}
        response = client.post(
            "/api/v1/admin/flags/background-job/throttle",
            json=payload,
            headers=admin_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["job_type"] == "calendar_sync"
        assert data["throttled"] is True

    @patch("routes.feature_flags_admin.LatencyPercentileTracker.from_redis")
    def test_get_latency_percentiles_with_data(
        self, mock_from_redis, client, admin_headers
    ):
        """Verify retrieval of latency percentiles for an operation."""
        mock_from_redis.return_value = {
            "p50": 120.5,
            "p95": 280.0,
            "p99": 410.2,
            "is_anomaly": False,
        }

        response = client.get(
            "/api/v1/admin/flags/latency-percentiles/schedule_meeting",
            headers=admin_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["operation"] == "schedule_meeting"
        assert data["p95"] == 280.0

    @patch("routes.feature_flags_admin.LatencyPercentileTracker.from_redis")
    def test_get_latency_percentiles_no_data(
        self, mock_from_redis, client, admin_headers
    ):
        """Verify retrieval of latency percentiles when no records exist."""
        mock_from_redis.return_value = None

        response = client.get(
            "/api/v1/admin/flags/latency-percentiles/unknown_op",
            headers=admin_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "no_data"
        assert data["operation"] == "unknown_op"

    @patch("routes.feature_flags_admin.set_feature_enabled")
    def test_toggle_feature_success(self, mock_toggle, client, admin_headers):
        """Verify enabling/disabling a feature flag via toggle endpoint."""
        mock_toggle.return_value = {"applied": True}

        response = client.post(
            "/api/v1/admin/flags/feature/toggle?feature_name=gemini_pro&enabled=true",
            headers=admin_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["feature"] == "gemini_pro"
        assert data["enabled"] is True
        mock_toggle.assert_called_once_with("gemini_pro", True)
