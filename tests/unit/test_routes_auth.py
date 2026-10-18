"""
Unit tests for routes/auth.py
"""

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from models.auth_db import OAuthTokenDB
from models.user_db import UserDB
from routes.auth import router


@pytest.mark.unit
class TestAuthRoutes:
    @pytest.fixture
    def app(self):
        """Create a dedicated FastAPI test application with SessionMiddleware and auth router."""
        app = FastAPI()
        app.add_middleware(
            SessionMiddleware,
            secret_key="test-session-secret-key-for-unit-tests",
        )
        app.include_router(router)
        return app

    @pytest.fixture
    def client(self, app):
        """Provide a TestClient instance maintaining cookie sessions."""
        return TestClient(app)

    def test_login_redirect_generates_state_and_google_url(self, client):
        """Verify /auth/login issues a redirect to Google's OAuth endpoint with state and expected query params."""
        response = client.get("/auth/login", follow_redirects=False)

        assert response.status_code == 307
        redirect_url = response.headers.get("location")
        assert redirect_url is not None

        parsed = urlparse(redirect_url)
        assert parsed.netloc == "accounts.google.com"
        assert parsed.path == "/o/oauth2/v2/auth"

        params = parse_qs(parsed.query)
        assert "client_id" in params
        assert "redirect_uri" in params
        assert params["response_type"] == ["code"]
        assert "state" in params
        assert len(params["state"][0]) > 10

    def test_callback_fails_on_missing_session_state(self, client):
        """Verify callback rejects requests with HTTP 400 when session has no oauth_state."""
        response = client.get("/auth/callback?code=mock_code&state=some_state")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid OAuth state"

    def test_callback_fails_on_mismatched_state(self, client):
        """Verify callback rejects requests when provided state differs from session state."""
        # Initiate login to populate session with a generated state
        login_res = client.get("/auth/login", follow_redirects=False)
        assert login_res.status_code == 307

        # Provide a different state
        response = client.get("/auth/callback?code=mock_code&state=tampered_state")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid OAuth state"

    @patch("routes.auth.AsyncSessionLocal")
    @patch("routes.auth.id_token.verify_oauth2_token")
    @patch("routes.auth.httpx.AsyncClient")
    def test_callback_success_new_user(
        self, mock_client_cls, mock_verify_token, mock_session_cls, client
    ):
        """Verify complete OAuth callback flow creating a new user and token record."""
        # 1. Trigger login to establish legitimate state in session
        login_resp = client.get("/auth/login", follow_redirects=False)
        parsed_url = urlparse(login_resp.headers["location"])
        valid_state = parse_qs(parsed_url.query)["state"][0]

        # 2. Mock Google Token Exchange via httpx
        mock_http_client = AsyncMock()
        mock_post_resp = MagicMock()
        mock_post_resp.json.return_value = {
            "access_token": "ya29.mock_access_token",
            "refresh_token": "1//mock_refresh_token",
            "id_token": "mock.jwt.id_token",
        }
        mock_post_resp.raise_for_status = MagicMock()
        mock_http_client.post.return_value = mock_post_resp
        mock_http_client.__aenter__.return_value = mock_http_client
        mock_http_client.__aexit__.return_value = None
        mock_client_cls.return_value = mock_http_client

        # 3. Mock Google ID Token Verification
        mock_verify_token.return_value = {
            "sub": "google_sub_1001",
            "email": "newuser@example.com",
            "name": "New User",
            "picture": "https://example.com/avatar.png",
        }

        # 4. Mock Database Session interactions
        mock_db_session = AsyncMock()
        mock_session_cls.return_value.__aenter__.return_value = mock_db_session

        # User query returns None (new user), Token query returns None
        mock_user_res = MagicMock()
        mock_user_res.scalar_one_or_none.return_value = None
        mock_token_res = MagicMock()
        mock_token_res.scalar_one_or_none.return_value = None
        mock_db_session.execute.side_effect = [mock_user_res, mock_token_res]

        async def flush_side_effect():
            # simulate DB assigning primary key id to newly added user
            for call in mock_db_session.add.call_args_list:
                obj = call[0][0]
                if isinstance(obj, UserDB):
                    obj.id = 101

        mock_db_session.flush.side_effect = flush_side_effect

        response = client.get(f"/auth/callback?code=valid_code&state={valid_state}")
        assert response.status_code == 200

        data = response.json()
        assert data["authenticated"] is True
        assert data["user"]["email"] == "newuser@example.com"
        assert data["user"]["name"] == "New User"
        assert data["user"]["id"] == 101
        assert mock_db_session.commit.called

    @patch("routes.auth.AsyncSessionLocal")
    @patch("routes.auth.id_token.verify_oauth2_token")
    @patch("routes.auth.httpx.AsyncClient")
    def test_callback_success_existing_user(
        self, mock_client_cls, mock_verify_token, mock_session_cls, client
    ):
        """Verify callback flow updating an existing user and renewing tokens."""
        login_resp = client.get("/auth/login", follow_redirects=False)
        valid_state = parse_qs(urlparse(login_resp.headers["location"]).query)["state"][
            0
        ]

        mock_http_client = AsyncMock()
        mock_post_resp = MagicMock()
        mock_post_resp.json.return_value = {
            "access_token": "ya29.renewed_token",
            "refresh_token": "1//renewed_refresh",
            "id_token": "mock.jwt.id_token",
        }
        mock_post_resp.raise_for_status = MagicMock()
        mock_http_client.post.return_value = mock_post_resp
        mock_http_client.__aenter__.return_value = mock_http_client
        mock_http_client.__aexit__.return_value = None
        mock_client_cls.return_value = mock_http_client

        mock_verify_token.return_value = {
            "sub": "google_sub_2002",
            "email": "existing@example.com",
            "name": "Existing User",
            "picture": "https://example.com/existing.png",
        }

        mock_db_session = AsyncMock()
        mock_session_cls.return_value.__aenter__.return_value = mock_db_session

        existing_user = UserDB(
            id=202,
            google_sub="google_sub_2002",
            email="old_email@example.com",
            name="Old Name",
        )
        existing_token = OAuthTokenDB(
            id=55,
            user_id=202,
            access_token="old_access",
            refresh_token="old_refresh",
            client_id="cid",
            client_secret="sec",
            scopes=[],
        )

        mock_user_res = MagicMock()
        mock_user_res.scalar_one_or_none.return_value = existing_user
        mock_token_res = MagicMock()
        mock_token_res.scalar_one_or_none.return_value = existing_token
        mock_db_session.execute.side_effect = [mock_user_res, mock_token_res]

        response = client.get(f"/auth/callback?code=valid_code&state={valid_state}")
        assert response.status_code == 200

        data = response.json()
        assert data["authenticated"] is True
        assert data["user"]["id"] == 202
        assert data["user"]["email"] == "existing@example.com"
        assert existing_token.access_token == "ya29.renewed_token"
        assert existing_token.refresh_token == "1//renewed_refresh"

    @patch("routes.auth.httpx.AsyncClient")
    def test_callback_token_endpoint_network_error(self, mock_client_cls, client):
        """Verify error propagation when Google token exchange endpoint fails."""
        import httpx

        login_resp = client.get("/auth/login", follow_redirects=False)
        valid_state = parse_qs(urlparse(login_resp.headers["location"]).query)["state"][
            0
        ]

        mock_http_client = AsyncMock()
        mock_post_resp = MagicMock()
        mock_post_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Bad Request",
            request=MagicMock(),
            response=MagicMock(status_code=400),
        )
        mock_http_client.post.return_value = mock_post_resp
        mock_http_client.__aenter__.return_value = mock_http_client
        mock_http_client.__aexit__.return_value = None
        mock_client_cls.return_value = mock_http_client

        with pytest.raises(httpx.HTTPStatusError):
            client.get(f"/auth/callback?code=bad_code&state={valid_state}")

    def test_logout_clears_session(self, client):
        """Verify /auth/logout clears user session and returns unauthenticated status."""
        response = client.post("/auth/logout")
        assert response.status_code == 200
        assert response.json() == {"authenticated": False}
