"""
Unit tests for routes/tenant_resources.py
"""

from unittest.mock import MagicMock
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from routes.tenant_resources import router
from utilities.security import get_audit_logger, get_current_tenant_context


@pytest.mark.unit
class TestTenantResources:
    @pytest.fixture
    def mock_audit_logger(self):
        """Provide a mock AuditLogger instance."""
        mock_logger = MagicMock()
        mock_logger.log.return_value = {
            "event_id": "audit_evt_101",
            "event_type": "tenant_resource_mutated",
            "action": "update_resource",
            "resource": "mock_resource_id",
        }
        return mock_logger

    @pytest.fixture
    def app(self, mock_audit_logger):
        """Build test application with dependency overrides."""
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_tenant_context] = lambda: {
            "tenant_id": "tenant_prod_77",
            "fallback_mode": False,
        }
        app.dependency_overrides[get_audit_logger] = lambda: mock_audit_logger
        return app

    @pytest.fixture
    def client(self, app):
        """Provide TestClient for tenant resources endpoints."""
        return TestClient(app)

    def test_get_tenant_info_standard_context(self, client):
        """Verify tenant retrieval returns context claims and access granted."""
        response = client.get("/api/v1/tenant-resources/")
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Tenant access granted"
        assert data["tenant_id"] == "tenant_prod_77"
        assert data["fallback_mode"] is False

    def test_get_tenant_info_fallback_mode(self, app):
        """Verify tenant retrieval correctly reflects fallback mode flag."""
        app.dependency_overrides[get_current_tenant_context] = lambda: {
            "tenant_id": "tenant_degraded",
            "fallback_mode": True,
        }
        client = TestClient(app)

        response = client.get("/api/v1/tenant-resources/")
        assert response.status_code == 200
        data = response.json()
        assert data["tenant_id"] == "tenant_degraded"
        assert data["fallback_mode"] is True

    def test_get_tenant_info_unauthenticated_fails(self):
        """Verify unauthenticated request without context dependency raises HTTP 401."""
        app = FastAPI()
        app.include_router(router)

        def raise_unauthorized():
            raise HTTPException(status_code=401, detail="Tenant context required")

        app.dependency_overrides[get_current_tenant_context] = raise_unauthorized
        client = TestClient(app)

        response = client.get("/api/v1/tenant-resources/")
        assert response.status_code == 401
        assert response.json()["detail"] == "Tenant context required"

    def test_perform_tenant_action_success(self, client, mock_audit_logger):
        """Verify tenant-scoped mutation triggers audit log with extracted payload keys."""
        payload = {"name": "Executive Boardroom", "seats": 20, "video_enabled": True}
        response = client.post("/api/v1/tenant-resources/action", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["audit_emitted"]["event_id"] == "audit_evt_101"

        mock_audit_logger.log.assert_called_once_with(
            event_type="tenant_resource_mutated",
            action="update_resource",
            resource="mock_resource_id",
            metadata={"payload_keys": ["name", "seats", "video_enabled"]},
        )

    def test_perform_tenant_action_empty_payload(self, client, mock_audit_logger):
        """Verify mutation with empty payload passes empty payload keys to audit logger."""
        response = client.post("/api/v1/tenant-resources/action", json={})
        assert response.status_code == 200
        assert response.json()["status"] == "success"

        mock_audit_logger.log.assert_called_once_with(
            event_type="tenant_resource_mutated",
            action="update_resource",
            resource="mock_resource_id",
            metadata={"payload_keys": []},
        )

    def test_perform_tenant_action_invalid_payload_type(self, client):
        """Verify sending non-dictionary payload yields HTTP 422 Unprocessable Entity."""
        response = client.post(
            "/api/v1/tenant-resources/action",
            content="non-json-string-payload",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422
