"""
Add unit tests covering the merged tenant context middleware.
"""

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from middleware.tenant_context import TenantContextMiddleware
from utilities.security import build_tenant_context
import redis

app = FastAPI()


class MockRedis:
    def __init__(self):
        self.data = {}
        self.is_up = True

    def ping(self):
        if not self.is_up:
            raise redis.ConnectionError("Redis is down")
        return True

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def incr(self, key):
        self.data[key] = self.data.get(key, 0) + 1
        return self.data[key]

    def expire(self, key, time):
        pass


mock_redis = MockRedis()
app.add_middleware(
    TenantContextMiddleware,
    redis_client=mock_redis,
    global_rate_limit=5,
    cluster_nodes=2,
)


@app.get("/test")
async def tenant_test_endpoint(request: Request):
    return {
        "tenant_context": getattr(request.state, "tenant_context", None),
        "tenant_claims": getattr(request.state, "tenant_claims", None),
        "tenant_id": getattr(request.state, "tenant_id", None),
        "user_id": getattr(request.state, "user_id", None),
    }


client = TestClient(app)


def test_missing_headers():
    response = client.get("/test")
    assert response.status_code == 401
    assert "Tenant context required" in response.json()["detail"]

    response = client.get("/test", headers={"X-Tenant-ID": "tenant1"})
    assert response.status_code == 401
    assert "Missing Authorization header" in response.json()["detail"]


def test_valid_request():
    response = client.get(
        "/test", headers={"X-Tenant-ID": "tenant1", "Authorization": "token123"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["tenant_context"]["tenant_id"] == "tenant1"
    assert payload["tenant_context"]["authenticated"] is True
    assert payload["tenant_context"]["fallback_mode"] is False
    assert payload["tenant_id"] == "tenant1"
    assert payload["tenant_claims"] == {}


def test_jwt_claims_produce_single_context():
    token = build_tenant_context(
        "tenant-jwt", scopes=["calendar:read", "calendar:write"]
    )
    response = client.get(
        "/test",
        headers={"X-Tenant-ID": "tenant-jwt", "X-Tenant-JWT": token},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["tenant_context"]["tenant_id"] == "tenant-jwt"
    assert payload["tenant_context"]["scopes"] == ["calendar:read", "calendar:write"]
    assert payload["tenant_claims"]["tenant_id"] == "tenant-jwt"
    assert payload["tenant_id"] == "tenant-jwt"


def test_jwt_and_header_tenant_mismatch_rejected():
    token = build_tenant_context("tenant-jwt")
    response = client.get(
        "/test",
        headers={"X-Tenant-ID": "tenant-other", "X-Tenant-JWT": token},
    )
    assert response.status_code == 403
    assert "Tenant ID mismatch" in response.json()["detail"]


def test_revoked_token():
    token = "revoked123"
    hashed = __import__("hashlib").sha256(token.encode("utf-8")).hexdigest()
    mock_redis.set(f"revoked_token:{hashed}", "1")
    response = client.get(
        "/test", headers={"X-Tenant-ID": "tenant1", "Authorization": token}
    )
    assert response.status_code == 403
    assert "Token revoked" in response.json()["detail"]


def test_global_rate_limit():
    for _ in range(5):
        client.get(
            "/test", headers={"X-Tenant-ID": "tenant_gl", "Authorization": "token123"}
        )

    response = client.get(
        "/test", headers={"X-Tenant-ID": "tenant_gl", "Authorization": "token123"}
    )
    assert response.status_code == 429
    assert "Global Rate Limit Exceeded" in response.json()["detail"]


def test_fallback_mode_local_rate_limit():
    mock_redis.is_up = False

    for _ in range(2):
        res = client.get(
            "/test", headers={"X-Tenant-ID": "tenant_loc", "Authorization": "token123"}
        )
        assert res.status_code == 200
        assert res.json()["tenant_context"]["fallback_mode"] is True

    response = client.get(
        "/test", headers={"X-Tenant-ID": "tenant_loc", "Authorization": "token123"}
    )
    assert response.status_code == 429
    assert "Local Rate Limit Exceeded" in response.json()["detail"]


if __name__ == "__main__":
    test_missing_headers()
    test_valid_request()
    test_jwt_claims_produce_single_context()
    test_jwt_and_header_tenant_mismatch_rejected()
    test_revoked_token()
    test_global_rate_limit()
    test_fallback_mode_local_rate_limit()
    print("All tests passed!")
