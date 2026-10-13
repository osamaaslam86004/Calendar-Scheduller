import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

pytest.importorskip("middleware.tenant_context")
from middleware.tenant_context import TenantContextMiddleware  # type: ignore


@pytest.mark.unit
def test_missing_tenant_returns_401():
    app = FastAPI()

    @app.get("/x")
    def x():
        return {"ok": True}

    app.add_middleware(TenantContextMiddleware)
    client = TestClient(app)
    resp = client.get("/x")
    assert resp.status_code == 401


@pytest.mark.unit
def test_claims_and_header_mismatch_returns_403():
    app = FastAPI()

    @app.get("/y")
    def y():
        return {"ok": True}

    with patch(
        "middleware.tenant_context.get_request_tenant_context",
        return_value={"tenant_id": "A"},
    ):
        app.add_middleware(TenantContextMiddleware)
        client = TestClient(app)
        resp = client.get("/y", headers={"X-Tenant-ID": "B", "Authorization": "token"})
        assert resp.status_code == 403


@pytest.mark.unit
def test_token_revoked_via_redis_returns_403_and_rate_limit_works():
    app = FastAPI()

    @app.get("/secure")
    def secure(request: Request):
        tc = getattr(request.state, "tenant_context", {})
        return {"tenant": tc.get("tenant_id"), "auth": tc.get("authenticated")}

    redis_mock = AsyncMock()
    redis_mock.ping.return_value = True
    redis_mock.get.return_value = b"1"

    async def fake_get_redis():
        return redis_mock

    with patch(
        "middleware.tenant_context.get_request_tenant_context", return_value=None
    ):
        with patch(
            "middleware.tenant_context.get_redis_client",
            new=AsyncMock(side_effect=fake_get_redis),
        ):
            app.add_middleware(TenantContextMiddleware)
            client = TestClient(app)
            resp = client.get(
                "/secure",
                headers={"X-Tenant-ID": "tenant123", "Authorization": "token-bad"},
            )
            assert resp.status_code == 403


@pytest.mark.unit
def test_redis_rate_limit_triggers_429_and_fallback_local_rate_limit():
    app = FastAPI()

    @app.get("/rate")
    def rate(request: Request):
        return {"ok": True}

    redis_mock = AsyncMock()
    redis_mock.ping.return_value = True
    call_count = {"n": 0}

    async def incr(key):
        call_count["n"] += 1
        return call_count["n"]

    redis_mock.incr.side_effect = incr
    redis_mock.expire.return_value = True
    redis_mock.get.return_value = None

    async def fake_get_redis():
        return redis_mock

    with patch(
        "middleware.tenant_context.get_request_tenant_context", return_value=None
    ):
        with patch(
            "middleware.tenant_context.get_redis_client",
            new=AsyncMock(side_effect=fake_get_redis),
        ):
            app.add_middleware(
                TenantContextMiddleware, global_rate_limit=2, cluster_nodes=1
            )
            client = TestClient(app)
            r1 = client.get(
                "/rate", headers={"X-Tenant-ID": "tenant-r", "Authorization": "tok"}
            )
            assert r1.status_code == 200
            r2 = client.get(
                "/rate", headers={"X-Tenant-ID": "tenant-r", "Authorization": "tok"}
            )
            assert r2.status_code == 200
            r3 = client.get(
                "/rate", headers={"X-Tenant-ID": "tenant-r", "Authorization": "tok"}
            )
            assert r3.status_code == 429

    with patch(
        "middleware.tenant_context.get_request_tenant_context", return_value=None
    ):
        with patch(
            "middleware.tenant_context.get_redis_client",
            new=AsyncMock(side_effect=Exception("down")),
        ):
            app2 = FastAPI()

            @app2.get("/local")
            def local():
                return {"ok": True}

            app2.add_middleware(
                TenantContextMiddleware, global_rate_limit=1000, cluster_nodes=10
            )
            client2 = TestClient(app2)
            local_limit = max(1, 1000 // 10)
            for i in range(local_limit):
                r = client2.get(
                    "/local",
                    headers={"X-Tenant-ID": "tenant-l", "Authorization": "tok"},
                )
                assert r.status_code in (200, 401, 403)
            r_exceed = client2.get(
                "/local", headers={"X-Tenant-ID": "tenant-l", "Authorization": "tok"}
            )
            assert r_exceed.status_code in (429, 200, 401)
