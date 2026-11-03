"""Performance-suite-only safety fixtures: synthetic settings and fake Redis clients."""

from __future__ import annotations

import tempfile
import warnings

import pytest

from tests.redis_fakes import FakeAsyncRedis, FakeSyncRedis

warnings.filterwarnings(
    "ignore",
    message="Using httpx with starlette.testclient is deprecated.*",
    category=DeprecationWarning,
)


TEST_ENV = {
    "SECURITY__SESSION_SECRET": "performance-tests-session-secret",
    "SECURITY__APP_MASTER_KEY": "0123456789abcdef" * 4,
    "DB__ASYNC_URL": "postgresql+asyncpg://perf_user:perf_password@127.0.0.1/perf_db",
    "DB__SYNC_URL": "postgresql+psycopg2://perf_user:perf_password@127.0.0.1/perf_db",
    "REDIS__URL": "redis://127.0.0.1:6379/0",
    "GOOGLE_OAUTH__CLIENT_ID": "performance-tests.apps.googleusercontent.com",
    "GOOGLE_OAUTH__CLIENT_SECRET": "performance-tests-google-secret",
    "GOOGLE_OAUTH__REDIRECT_URL": "http://localhost:8000/oauth/callback",
    "LLM__GEMINI_API_KEY": "performance-tests-gemini-key",
    "LLM__OPEN_ROUTER_API_KEY": "performance-tests-openrouter-key",
    "APP__ENV": "development",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HOME": tempfile.mkdtemp(prefix="scheduler-perf-hf-home-"),
}


@pytest.fixture(autouse=True)
def isolate_performance_runtime(monkeypatch):
    """Keep all perf tests offline and independent of a developer's local credentials."""
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    FakeSyncRedis.store = {}
    # Fake clients patch only for tests collected under tests/performance.
    try:
        import redis

        monkeypatch.setattr(
            redis.Redis,
            "from_url",
            classmethod(lambda _cls, *args, **kwargs: FakeSyncRedis()),
            raising=False,
        )
        monkeypatch.setattr(
            redis, "from_url", lambda *args, **kwargs: FakeSyncRedis(), raising=False
        )
        try:
            import redis.asyncio as aioredis

            monkeypatch.setattr(
                aioredis,
                "from_url",
                lambda *args, **kwargs: FakeAsyncRedis(),
                raising=False,
            )
        except Exception:
            pass
    except Exception:
        # Import-related tests report absent Redis as an optional dependency skip.
        pass
    yield
