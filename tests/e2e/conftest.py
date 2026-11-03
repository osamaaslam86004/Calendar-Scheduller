"""Pytest fixtures and configuration for E2E tests of Calendar Scheduling API."""

import base64
import json
import os
import sys
import warnings
from collections.abc import AsyncGenerator, Generator

import itsdangerous
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

warnings.filterwarnings(
    "ignore",
    message="Using httpx with starlette.testclient is deprecated.*",
    category=DeprecationWarning,
)

sys.path.insert(0, os.path.abspath("."))

from config.database import get_db
from config.settings import settings
from main import app
from models.base import Base
from models.user_db import UserDB
from tests.redis_fakes import FakeAsyncRedis, FakeSyncRedis
from utilities.security import build_tenant_context

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


def sign_session_cookie(session_data: dict) -> str:
    """Build the cookie value SessionMiddleware would have written for this session."""
    payload = base64.b64encode(json.dumps(session_data).encode("utf-8"))
    signer = itsdangerous.TimestampSigner(str(settings.security.session_secret))
    return signer.sign(payload).decode("utf-8")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Specify asyncio as the async backend for pytest."""
    return "asyncio"


@pytest.fixture(autouse=True)
def isolate_redis(monkeypatch):
    """Keep every e2e test offline: Redis is replaced by the shared in-memory fakes."""
    FakeSyncRedis.store = {}
    monkeypatch.setattr(
        "redis.asyncio.from_url", lambda *args, **kwargs: FakeAsyncRedis()
    )
    monkeypatch.setattr("config.cache._redis_client", None, raising=False)
    yield


@pytest_asyncio.fixture(scope="function")
async def test_db_engine():
    """Create an isolated in-memory SQLite database engine for testing."""
    # StaticPool keeps one connection so every session sees the same in-memory database.
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(test_db_engine) -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional SQLAlchemy AsyncSession for tests."""
    session_maker = async_sessionmaker(
        test_db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_maker() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def mock_app_deps(db_session, test_db_engine, monkeypatch):
    """Override FastAPI database dependency and session maker."""

    async def _get_test_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _get_test_db
    # auth.get_current_user opens its own session, so point that factory at the test DB too.
    monkeypatch.setattr(
        "auth.AsyncSessionLocal",
        async_sessionmaker(test_db_engine, class_=AsyncSession, expire_on_commit=False),
    )
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(scope="function")
async def async_client(mock_app_deps) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP test client bound to the FastAPI application."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Content-Type": "application/json"},
    ) as client:
        yield client


@pytest.fixture
def ws_client() -> Generator[TestClient, None, None]:
    """Synchronous Starlette TestClient for WebSocket testing."""
    with TestClient(app) as client:
        yield client


@pytest_asyncio.fixture(scope="function")
async def test_user(db_session: AsyncSession) -> UserDB:
    """Create and return a persistable test user model."""
    user = UserDB(
        google_sub="google-sub-e2e-12345",
        email="e2e_user@example.com",
        name="E2E Test User",
        picture="https://example.com/avatar.jpg",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture(scope="function")
async def authenticated_client(
    async_client: AsyncClient, test_user: UserDB
) -> AsyncClient:
    """Return an HTTPX AsyncClient configured with an authenticated session."""
    tenant_token = build_tenant_context(
        tenant_id=test_user.id,
        scopes=["calendar:read", "calendar:write", "dlq:replay"],
        operation="auth",
        entity_key=f"user:{test_user.id}",
        bounds={
            "user_id": test_user.id,
            "allowed_operations": ["schedule", "replay", "audit"],
        },
    )
    # The app reads identity from its signed session cookie, so mint a real one.
    async_client.cookies.set(
        "session",
        sign_session_cookie({"user_id": test_user.id, "tenant_context": tenant_token}),
    )
    async_client.headers.update({"X-Tenant-Context": tenant_token})
    return async_client


@pytest.fixture
def admin_headers() -> dict[str, str]:
    """Return valid administrative headers for restricted endpoints."""
    return {
        "X-Admin-Key": settings.security.app_master_key,
        "Authorization": f"Bearer {settings.security.app_master_key}",
    }
