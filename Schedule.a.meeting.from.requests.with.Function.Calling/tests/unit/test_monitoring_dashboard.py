"""
Unit tests for routes/monitoring_dashboard.py
"""

from datetime import datetime, timezone
import json
from unittest.mock import MagicMock, patch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.monitoring_dashboard import router


@pytest.mark.unit
class TestMonitoringDashboard:
    @pytest.fixture
    def app(self):
        """Build test application mounting monitoring_dashboard router."""
        app = FastAPI()
        app.include_router(router)
        return app

    @pytest.fixture
    def client(self, app):
        """Provide TestClient for monitoring dashboard endpoints."""
        return TestClient(app)

    @patch("routes.monitoring_dashboard._redis")
    def test_health_snapshot_with_metrics(self, mock_r, client):
        """Verify health snapshot aggregates monitored operations and circuit breaker states."""
        mock_r.keys.return_value = [
            "latency:percentiles:schedule_meeting",
            "latency:percentiles:gemini_extract",
        ]

        metric_sched = {
            "p50": 110.0,
            "p95": 250.0,
            "p99": 400.0,
            "mean": 125.0,
            "std_dev": 25.0,
            "sample_count": 500,
            "anomaly_threshold": 300.0,
            "is_anomaly": False,
        }
        metric_extract = {
            "p50": 300.0,
            "p95": 850.0,
            "p99": 1200.0,
            "mean": 450.0,
            "std_dev": 150.0,
            "sample_count": 200,
            "anomaly_threshold": 600.0,
            "is_anomaly": True,
        }

        def mock_get(key):
            if key == "latency:percentiles:schedule_meeting":
                return json.dumps(metric_sched)
            if key == "latency:percentiles:gemini_extract":
                return json.dumps(metric_extract)
            if key == "circuit_breaker:llm_provider:state":
                return "HALF_OPEN"
            if key == "circuit_breaker:google_calendar:state":
                return "CLOSED"
            return None

        mock_r.get.side_effect = mock_get

        response = client.get("/api/v1/monitoring/health-snapshot")
        assert response.status_code == 200
        data = response.json()

        assert data["total_anomalies"] == 1
        assert len(data["operations_monitored"]) == 2
        assert "schedule_meeting" in data["metrics"]
        assert "gemini_extract" in data["metrics"]
        assert data["circuit_breaker_state"]["llm_provider"] == "HALF_OPEN"
        assert data["circuit_breaker_state"]["google_calendar"] == "CLOSED"

    @patch("routes.monitoring_dashboard._redis")
    def test_health_snapshot_empty_system(self, mock_r, client):
        """Verify health snapshot handles zero monitored operations cleanly."""
        mock_r.keys.return_value = []

        response = client.get("/api/v1/monitoring/health-snapshot")
        assert response.status_code == 200
        data = response.json()
        assert data["total_anomalies"] == 0
        assert data["operations_monitored"] == []
        assert data["metrics"] == {}

    @patch("routes.monitoring_dashboard.LatencyPercentileTracker")
    def test_get_percentiles_for_operation_found(self, mock_tracker_cls, client):
        """Verify retrieving percentiles for a specific active operation."""
        mock_tracker = MagicMock()
        mock_tracker.from_redis.return_value = {
            "p50": 85.0,
            "p95": 190.0,
            "p99": 280.0,
            "mean": 95.0,
            "std_dev": 18.0,
            "sample_count": 1200,
            "anomaly_threshold": 250.0,
            "is_anomaly": False,
        }
        mock_tracker_cls.return_value = mock_tracker

        response = client.get("/api/v1/monitoring/percentiles/calendar_sync")
        assert response.status_code == 200
        data = response.json()
        assert data["operation"] == "calendar_sync"
        assert data["p95"] == 190.0
        assert data["is_anomaly"] is False

    @patch("routes.monitoring_dashboard.LatencyPercentileTracker")
    def test_get_percentiles_for_operation_not_found(self, mock_tracker_cls, client):
        """Verify requesting unknown operation returns HTTP 404."""
        mock_tracker = MagicMock()
        mock_tracker.from_redis.return_value = None
        mock_tracker_cls.return_value = mock_tracker

        response = client.get("/api/v1/monitoring/percentiles/non_existent_op")
        assert response.status_code == 404
        assert "No metrics found" in response.json()["detail"]

    @patch("routes.monitoring_dashboard._redis")
    def test_get_current_anomalies(self, mock_r, client):
        """Verify retrieval of all operations flagged as anomalies."""
        mock_r.keys.return_value = [
            "latency:percentiles:op_normal",
            "latency:percentiles:op_spiking",
        ]

        def mock_get(key):
            if key == "latency:percentiles:op_normal":
                return json.dumps({"p50": 10.0, "p95": 20.0, "is_anomaly": False})
            if key == "latency:percentiles:op_spiking":
                return json.dumps(
                    {
                        "p50": 100.0,
                        "p95": 900.0,
                        "p99": 1500.0,
                        "mean": 300.0,
                        "std_dev": 200.0,
                        "sample_count": 50,
                        "anomaly_threshold": 400.0,
                        "is_anomaly": True,
                    }
                )
            return None

        mock_r.get.side_effect = mock_get

        response = client.get("/api/v1/monitoring/anomalies")
        assert response.status_code == 200
        anomalies = response.json()
        assert len(anomalies) == 1
        assert anomalies[0]["operation"] == "op_spiking"
        assert anomalies[0]["is_anomaly"] is True

    @patch("routes.monitoring_dashboard.get_oauth_grace_period_hours")
    @patch("routes.monitoring_dashboard.get_circuit_breaker_threshold")
    @patch("routes.monitoring_dashboard.get_rate_limit_capacity")
    @patch("routes.monitoring_dashboard._redis")
    def test_get_policy_state(
        self, mock_r, mock_rl_cap, mock_cb_thresh, mock_oauth_grace, client
    ):
        """Verify inspection of active dynamic policies, rate limits, and thresholds."""
        mock_rl_cap.side_effect = lambda p: {
            "default": 100,
            "openai": 60,
            "google_calendar": 30,
        }.get(p, 50)
        mock_cb_thresh.side_effect = lambda p: 5
        mock_oauth_grace.return_value = 24
        mock_r.get.return_value = "75"

        response = client.get("/api/v1/monitoring/policy-state")
        assert response.status_code == 200
        data = response.json()
        assert "timestamp" in data
        assert data["rate_limits"]["default"] == 100
        assert data["circuit_breaker_thresholds"]["llm_provider"] == 5
        assert data["oauth_grace_period"] == 24
        assert "token_refill_rates" in data

    @patch("routes.monitoring_dashboard._redis")
    def test_get_metrics_history_success(self, mock_r, client):
        """Verify historical metrics points are parsed, filtered, and sorted chronologically."""
        now_iso = datetime.utcnow().isoformat()
        stream_data = [
            (
                "1-0",
                {
                    "timestamp": now_iso,
                    "p50": "90.0",
                    "p95": "180.0",
                    "p99": "240.0",
                    "mean": "100.0",
                    "is_anomaly": "false",
                },
            ),
            (
                "2-0",
                {
                    "timestamp": now_iso,
                    "p50": "110.0",
                    "p95": "290.0",
                    "p99": "400.0",
                    "mean": "130.0",
                    "is_anomaly": "true",
                },
            ),
        ]
        mock_r.xrange.return_value = stream_data

        response = client.get(
            "/api/v1/monitoring/metrics-history/schedule_meeting?time_window_minutes=60"
        )
        assert response.status_code == 200
        history = response.json()
        assert len(history) == 2
        assert history[0]["p50"] == 90.0
        assert history[1]["is_anomaly"] is True

    @patch("routes.monitoring_dashboard._redis")
    def test_get_metrics_history_not_found(self, mock_r, client):
        """Verify history query returns HTTP 404 when stream has no entries."""
        mock_r.xrange.return_value = []

        response = client.get("/api/v1/monitoring/metrics-history/unknown_operation")
        assert response.status_code == 404
        assert "No history found" in response.json()["detail"]

    def test_get_metrics_history_invalid_time_window(self, client):
        """Verify validation error when time_window_minutes query parameter is outside 1..1440."""
        response_low = client.get(
            "/api/v1/monitoring/metrics-history/op?time_window_minutes=0"
        )
        assert response_low.status_code == 422

        response_high = client.get(
            "/api/v1/monitoring/metrics-history/op?time_window_minutes=5000"
        )
        assert response_high.status_code == 422

    def test_get_dashboard_html(self, client):
        """Verify dashboard HTML serves interactive Chart.js frontend interface."""
        response = client.get("/api/v1/monitoring/dashboard-html")
        assert response.status_code == 200
        assert "<!DOCTYPE html>" in response.text
        assert "Latency Monitoring Dashboard" in response.text
