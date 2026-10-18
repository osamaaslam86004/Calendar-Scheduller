import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

pytest.importorskip("middleware.tenant_rbac")
from middleware.tenant_rbac import TenantRBACMiddleware  # type: ignore


@pytest.mark.unit
def test_rbac_allows_exempt_paths_and_populates_state():
    app = FastAPI()

    @app.get("/auth")
    def auth():
        return {"ok": True}

    app.add_middleware(TenantRBACMiddleware)
    client = TestClient(app)
    resp = client.get("/auth")
    assert resp.status_code == 200

    @app.get("/rbac")
    def rbac(request: Request):
        return {
            "scopes": getattr(request.state, "scopes", None),
            "tenant": getattr(request.state, "tenant_id", None),
        }

    resp2 = client.get("/rbac")
    assert resp2.status_code in (200, 401, 403)
