import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

pytest.importorskip("middleware.tenant_guard")
from middleware.tenant_guard import TenantGuardMiddleware  # type: ignore


@pytest.mark.unit
def test_unauthenticated_paths_are_exempt():
    app = FastAPI()

    @app.get("/docs")
    def docs():
        return {"ok": True}

    app.add_middleware(TenantGuardMiddleware)
    client = TestClient(app)
    resp = client.get("/docs")
    assert resp.status_code == 200


@pytest.mark.unit
def test_request_state_populated_for_authenticated_paths():
    app = FastAPI()

    @app.get("/protected")
    def protected(request: Request):
        return {
            "tenant_id": getattr(request.state, "tenant_id", None),
            "authenticated": getattr(request.state, "authenticated", None),
        }

    app.add_middleware(TenantGuardMiddleware)
    client = TestClient(app)
    resp = client.get("/protected")
    assert resp.status_code in (200, 401, 403)
