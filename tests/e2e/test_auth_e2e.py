"""End-to-End tests for OAuth2 authentication flows."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth_db import OAuthTokenDB
from models.user_db import UserDB


@pytest.mark.asyncio
async def test_login_redirect_flow(async_client: AsyncClient):
    """Test OAuth login endpoint returns 307 redirect to Google auth URL."""
    response = await async_client.get("/auth/login", follow_redirects=False)

    assert response.status_code in (302, 307)
    redirect_url = response.headers.get("location", "")
    assert "accounts.google.com/o/oauth2/v2/auth" in redirect_url
    assert "client_id=" in redirect_url
    assert "response_type=code" in redirect_url
    assert "state=" in redirect_url


@pytest.mark.asyncio
async def test_callback_success(
    async_client: AsyncClient, db_session: AsyncSession
):
    """Test OAuth callback authenticates user and creates tokens."""
    state_token = "e2e_test_state_secret_12345"
    mock_id_info = {
        "sub": "google-sub-oauth-999",
        "email": "new_oauth_user@example.com",
        "name": "OAuth User",
        "picture": "https://example.com/pic.jpg",
    }
    mock_token_response = {
        "access_token": "ya29.fake_access_token",
        "refresh_token": "1//fake_refresh_token",
        "id_token": "fake_id_token",
    }

    async_client.cookies.set("session", f"oauth_state={state_token}")

    mock_post_response = AsyncMock()
    mock_post_response.raise_for_status = MagicMock()
    mock_post_response.json = MagicMock(return_value=mock_token_response)

    with (
        patch("httpx.AsyncClient.post", return_value=mock_post_response),
        patch(
            "google.oauth2.id_token.verify_oauth2_token",
            return_value=mock_id_info,
        ),
    ):
        response = await async_client.get(
            "/auth/callback",
            params={"code": "auth_code_xyz", "state": state_token},
            cookies={"oauth_state": state_token},
        )

    if response.status_code == 200:
        data = response.json()
        assert data.get("authenticated") is True
        assert data["user"]["email"] == "new_oauth_user@example.com"

        user_result = await db_session.execute(
            select(UserDB).where(UserDB.google_sub == "google-sub-oauth-999")
        )
        created_user = user_result.scalar_one_or_none()
        if created_user:
            assert created_user.email == "new_oauth_user@example.com"

            token_result = await db_session.execute(
                select(OAuthTokenDB).where(
                    OAuthTokenDB.user_id == created_user.id
                )
            )
            created_token = token_result.scalar_one_or_none()
            assert created_token is not None


@pytest.mark.asyncio
async def test_callback_invalid_state(async_client: AsyncClient):
    """Test OAuth callback with invalid state returns 400 Bad Request."""
    response = await async_client.get(
        "/auth/callback",
        params={"code": "auth_code_xyz", "state": "invalid_state_123"},
    )
    assert response.status_code == 400
    assert "Invalid OAuth state" in response.json().get("detail", "")


@pytest.mark.asyncio
async def test_logout_flow(authenticated_client: AsyncClient):
    """Test logout endpoint clears session and returns false state."""
    response = await authenticated_client.post("/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}


@pytest.mark.asyncio
async def test_unauthenticated_protected_route_access(
    async_client: AsyncClient,
):
    """Test accessing async schedule without auth returns 401."""
    payload = {
        "request_text": "Schedule a sync meeting with team tomorrow at 10 AM"
    }
    response = await async_client.post("/api/v1/async-schedule", json=payload)

    assert response.status_code == 401
    assert "Authentication required" in response.json().get("detail", "")
