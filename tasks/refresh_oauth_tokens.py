"""
OAuthTokenManager with:
1. Redis lock to prevent concurrent refresh races
2. 45s grace-period serving from Redis
3. token-family revocation on reuse/compromise
4. safe refresh flow and automatic quarantine logic

Integrated into the periodic refresh task so workers use the manager instead of
directly deleting/overwriting tokens unsafely
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import redis
import httpx
from celery_app import celery_app

# from google.auth.transport.requests import Request
# from google.oauth2.credentials import Credentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config.settings import settings
from models.auth_db import OAuthTokenDB
from utilities.cache import set_cached_function_call
from utilities.security import append_audit_event, get_current_correlation_id

logger = logging.getLogger(__name__)


ASYNC_DB_URL = str(settings.db.async_url)
REDIS_CLIENT = redis.Redis.from_url(settings.redis.url, decode_responses=True)


class SecurityException(RuntimeError):
    """Raised when a refresh token family is considered compromised."""


class OAuthTokenManager:
    """Coordinate safe OAuth token refreshes with Redis-based lock + grace period.

    This avoids the classic race where multiple workers refresh the same user token
    concurrently and delete the old refresh token before the other worker finishes.
    """

    def __init__(self, redis_client: redis.Redis, grace_period_seconds: int = 45):
        self.redis = redis_client
        self.grace_period = grace_period_seconds

    def _lock_key(self, user_id: str) -> str:
        return f"lock:oauth:refresh:{user_id}"

    def _grace_key(self, refresh_token: str) -> str:
        return f"oauth:grace:{refresh_token}"

    def _family_key(self, user_id: str) -> str:
        return f"oauth:token_family:{user_id}"

    def _revoked_key(self, user_id: str) -> str:
        return f"oauth:revoked_tokens:{user_id}"

    def _revoke_entire_token_family(self, user_id: str) -> None:
        family_key = self._family_key(user_id)
        members = self.redis.smembers(family_key)
        if members:
            self.redis.sadd(self._revoked_key(user_id), *members)
            # Keep the family lineage quarantined until manual remediation.
            self.redis.delete(family_key)

    def refresh_tokens_atomic(
        self, user_id: str, old_refresh_token: Optional[str]
    ) -> Dict[str, Any]:
        """Refresh tokens while preventing duplicate or unsafe refresh activity."""
        if not old_refresh_token:
            raise ValueError("old_refresh_token is required for rotation")

        grace_key = self._grace_key(old_refresh_token)
        cached_tokens = self.redis.hgetall(grace_key)
        if cached_tokens:
            return {
                "access_token": cached_tokens.get("access_token"),
                "refresh_token": cached_tokens.get("refresh_token"),
                "status": "GRACE_PERIOD_HIT",
            }

        lock_key = self._lock_key(user_id)
        acquired = self.redis.set(lock_key, "locked", nx=True, ex=30)
        if not acquired:
            time.sleep(0.5)
            return self.refresh_tokens_atomic(user_id, old_refresh_token)

        try:
            if self.redis.sismember(self._revoked_key(user_id), old_refresh_token):
                self._revoke_entire_token_family(user_id)
                raise SecurityException(
                    "Refresh token reuse detected. Entire token family revoked."
                )

            new_tokens = self._call_oauth_provider_refresh(old_refresh_token)

            pipe = self.redis.pipeline()
            pipe.hset(grace_key, mapping=new_tokens)
            pipe.expire(grace_key, self.grace_period)
            pipe.sadd(self._revoked_key(user_id), old_refresh_token)
            pipe.sadd(self._family_key(user_id), old_refresh_token)
            if new_tokens.get("refresh_token"):
                pipe.sadd(self._family_key(user_id), new_tokens["refresh_token"])
            pipe.execute()

            return {**new_tokens, "status": "REFRESHED"}
        finally:
            self.redis.delete(lock_key)

    def _call_oauth_provider_refresh(self, refresh_token: str) -> Dict[str, Any]:
        """Call Google OAuth refresh endpoint and return token dict."""
        data = {
            "client_id": settings.google_oauth.client_id,
            "client_secret": settings.google_oauth.client_secret.get_secret_value(),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                "https://oauth2.googleapis.com/token",
                data=data,
            )
            resp.raise_for_status()
            token_data = resp.json()

        expiry_dt = datetime.now(timezone.utc) + timedelta(
            seconds=int(token_data.get("expires_in", 3600))
        )

        return {
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token", refresh_token),
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": settings.google_oauth.client_id,
            "client_secret": settings.google_oauth.client_secret.get_secret_value(),
            "scopes": [
                "openid",
                "email",
                "profile",
                "https://www.googleapis.com/auth/calendar",
            ],
            "expiry": expiry_dt.isoformat(),
        }


oauth_token_manager = OAuthTokenManager(REDIS_CLIENT, grace_period_seconds=45)


def get_task_sessionmaker() -> tuple[object, async_sessionmaker[AsyncSession]]:
    _worker_engine = create_async_engine(ASYNC_DB_URL, poolclass=NullPool)
    _worker_sessionmaker = async_sessionmaker(
        _worker_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return _worker_engine, _worker_sessionmaker


async def _rotate_tokens(threshold_minutes: int = 10) -> int:
    """Refresh OAuth tokens that are expired or expiring within threshold_minutes.

    Returns number of tokens refreshed.
    """

    now = datetime.now(timezone.utc)
    threshold = now + timedelta(minutes=threshold_minutes)

    _worker_engine, _worker_sessionmaker = get_task_sessionmaker()
    refreshed_count = 0

    try:
        async with _worker_sessionmaker() as session:
            stmt = select(OAuthTokenDB).where(
                OAuthTokenDB.refresh_token.isnot(None),
                (OAuthTokenDB.expiry.is_(None)) | (OAuthTokenDB.expiry <= threshold),
            )

            result = await session.execute(stmt)
            tokens = result.scalars().all()

            for token in tokens:
                try:
                    refresh_result = oauth_token_manager.refresh_tokens_atomic(
                        str(token.user_id), token.refresh_token
                    )

                    if refresh_result.get("status") == "GRACE_PERIOD_HIT":
                        logger.info(
                            "Token refresh for user %s served from grace window; skipping DB write.",
                            token.user_id,
                        )
                        append_audit_event(
                            "oauth_grace_period_hit",
                            actor_id=token.user_id,
                            tenant_id=token.user_id,
                            action="oauth.refresh",
                            resource=f"user:{token.user_id}",
                            metadata={"status": "GRACE_PERIOD_HIT"},
                            request_id=str(token.user_id),
                            correlation_id=get_current_correlation_id(),
                        )
                        continue

                    access_token = refresh_result["access_token"]
                    new_refresh_token = refresh_result.get("refresh_token")
                    expiry = refresh_result.get("expiry")
                    if expiry:
                        expiry_dt = datetime.fromisoformat(expiry)
                    else:
                        expiry_dt = now + timedelta(hours=1)

                    token.access_token = access_token
                    token.refresh_token = new_refresh_token or token.refresh_token
                    token.expiry = expiry_dt
                    token.client_id = refresh_result.get("client_id", token.client_id)
                    token.client_secret = refresh_result.get(
                        "client_secret", token.client_secret
                    )
                    token.scopes = refresh_result.get("scopes", token.scopes)

                    append_audit_event(
                        "oauth_refresh_succeeded",
                        actor_id=token.user_id,
                        tenant_id=token.user_id,
                        action="oauth.refresh",
                        resource=f"user:{token.user_id}",
                        metadata={
                            "expiry": expiry_dt.isoformat(),
                            "status": "REFRESHED",
                        },
                        request_id=str(token.user_id),
                        correlation_id=get_current_correlation_id(),
                    )

                    cache_key = f"google_oauth_token:{token.user_id}"
                    cache_payload = {
                        "access_token": access_token,
                        "refresh_token": new_refresh_token or token.refresh_token,
                        "token_uri": refresh_result.get(
                            "token_uri", "https://oauth2.googleapis.com/token"
                        ),
                        "client_id": refresh_result.get("client_id", token.client_id),
                        "client_secret": refresh_result.get(
                            "client_secret", token.client_secret
                        ),
                        "scopes": refresh_result.get("scopes", token.scopes),
                        "expiry": expiry_dt.isoformat(),
                    }

                    await set_cached_function_call(
                        cache_key=cache_key,
                        data=cache_payload,
                        expire_seconds=3600,
                    )

                    refreshed_count += 1
                except SecurityException as exc:
                    logger.exception(
                        "OAuth token family revoked for user %s: %s",
                        token.user_id,
                        exc,
                    )
                    append_audit_event(
                        "oauth_refresh_revoked",
                        actor_id=token.user_id,
                        tenant_id=token.user_id,
                        action="oauth.refresh",
                        resource=f"user:{token.user_id}",
                        metadata={"status": "REVOKED", "reason": str(exc)},
                        request_id=str(token.user_id),
                        correlation_id=get_current_correlation_id(),
                    )
                    token.access_token = "REVOKED"
                    token.refresh_token = None
                    await session.commit()
                except Exception as exc:
                    logger.exception(
                        "Failed to refresh token for user %s: %s",
                        token.user_id,
                        exc,
                    )
                    append_audit_event(
                        "oauth_refresh_failed",
                        actor_id=token.user_id,
                        tenant_id=token.user_id,
                        action="oauth.refresh",
                        resource=f"user:{token.user_id}",
                        metadata={"status": "FAILED", "error": str(exc)},
                        request_id=str(token.user_id),
                        correlation_id=get_current_correlation_id(),
                    )

            await session.commit()

    finally:
        await _worker_engine.dispose()

    return refreshed_count


@celery_app.task(name="tasks.rotate_oauth_tokens")
def rotate_oauth_tokens():
    """Celery task wrapper that runs the async rotation routine.

    This task is intended to be scheduled periodically (beat).
    """
    try:
        refreshed = asyncio.run(_rotate_tokens(threshold_minutes=10))
        logger.info("Rotated %d OAuth tokens", refreshed)
        append_audit_event(
            "oauth_rotation_completed",
            actor_id="system",
            tenant_id="system",
            action="oauth.rotate",
            resource="oauth_tokens",
            metadata={"refreshed": refreshed},
            request_id="oauth_rotation",
            correlation_id=get_current_correlation_id(),
        )
        return {"refreshed": refreshed}
    except Exception as exc:
        logger.exception("OAuth rotation failed: %s", exc)
        append_audit_event(
            "oauth_rotation_failed",
            actor_id="system",
            tenant_id="system",
            action="oauth.rotate",
            resource="oauth_tokens",
            metadata={"error": str(exc)},
            request_id="oauth_rotation",
            correlation_id=get_current_correlation_id(),
        )
        raise
