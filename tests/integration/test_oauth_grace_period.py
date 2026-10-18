"""Integration tests for concurrent worker token rotation race conditions and grace period."""

import asyncio
from unittest.mock import patch
import pytest

from tasks.refresh_oauth_tokens import OAuthTokenManager, SecurityException


@pytest.mark.integration
@pytest.mark.anyio
class TestOAuthGracePeriodIntegration:
    async def test_concurrent_token_refresh_grace_window(self, sync_redis):
        """Simulate parallel workers presenting the same legacy refresh token simultaneously."""
        token_manager = OAuthTokenManager(
            redis_client=sync_redis, grace_period_seconds=10
        )
        user_id = "user_test_888"
        old_refresh_token = "refresh_token_legacy_v1"

        # Mock downstream OAuth provider call
        call_count = 0

        def mock_provider_call(token):
            nonlocal call_count
            call_count += 1
            return {
                "access_token": f"access_token_v2_call_{call_count}",
                "refresh_token": f"refresh_token_v2_call_{call_count}",
            }

        token_manager._call_oauth_provider_refresh = mock_provider_call

        # Execute 5 parallel refresh tasks using threads
        tasks = [
            asyncio.create_task(
                asyncio.to_thread(
                    token_manager.refresh_tokens_atomic,
                    user_id,
                    old_refresh_token,
                )
            )
            for _ in range(5)
        ]
        results = await asyncio.gather(*tasks)

        # Assertions
        assert len(results) == 5
        # Downstream provider should only be called once; others served from lock or grace window
        assert call_count == 1

        # All workers receive valid token pairs
        for res in results:
            assert res["access_token"] == "access_token_v2_call_1"
            assert res["refresh_token"] == "refresh_token_v2_call_1"

    async def test_out_of_bounds_token_reuse_triggers_family_revocation(
        self, sync_redis
    ):
        """Verify that presenting a revoked token revokes the entire token family."""
        token_manager = OAuthTokenManager(
            redis_client=sync_redis, grace_period_seconds=1
        )
        user_id = "user_test_999"
        old_token = "refresh_token_legacy_v1"

        call_count = 0

        def mock_provider_call(token):
            nonlocal call_count
            call_count += 1
            return {
                "access_token": f"access_v2_{call_count}",
                "refresh_token": f"refresh_v2_{call_count}",
            }

        token_manager._call_oauth_provider_refresh = mock_provider_call

        # First refresh -> Success
        res1 = token_manager.refresh_tokens_atomic(user_id, old_token)
        assert res1["access_token"] == "access_v2_1"

        # Simulate expiration of grace window key
        sync_redis.delete(token_manager._grace_key(old_token))

        # Manually add old_token to revoked set to simulate subsequent reuse after grace
        sync_redis.sadd(token_manager._revoked_key(user_id), old_token)

        # Attempt reuse after grace period expiry -> Must raise SecurityException
        with pytest.raises(SecurityException) as exc_info:
            token_manager.refresh_tokens_atomic(user_id, old_token)

        msg = str(exc_info.value).lower()
        assert "reuse detected" in msg or "family revoked" in msg
