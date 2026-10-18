# routes/auth.py
# This file handles the OAuth2 authentication flow with Google, including login, callback,
# and logout endpoints. It uses FastAPI's APIRouter to define the routes and SQLAlchemy for
# database interactions.

# This replaces the manual seed_oauth_token.py approach


import os
import secrets
from urllib.parse import urlencode

import httpx

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from sqlalchemy import select

from config.database import AsyncSessionLocal
from config.limiter import limiter
from config.settings import settings

from models.user_db import UserDB
from models.auth_db import OAuthTokenDB

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"],
)


GOOGLE_CLIENT_ID = settings.google_oauth.client_id
GOOGLE_CLIENT_SECRET = settings.google_oauth.client_secret.get_secret_value()
GOOGLE_REDIRECT_URI = str(settings.google_oauth.redirect_url)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/calendar",
]


@router.get("/login")
@limiter.limit("5/minute")  # Prevents auth abuse/bot spam
async def login(request: Request):

    state = secrets.token_urlsafe(32)

    request.session["oauth_state"] = state

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }

    url = f"{GOOGLE_AUTH_URL}?" f"{urlencode(params)}"

    return RedirectResponse(url)


@router.get("/callback")
@limiter.limit("10/minute")
async def callback(
    request: Request,
    code: str,
    state: str,
):

    expected_state = request.session.pop(
        "oauth_state",
        None,
    )

    if not expected_state or not secrets.compare_digest(
        state,
        expected_state,
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid OAuth state",
        )

    async with httpx.AsyncClient() as client:

        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )

    response.raise_for_status()

    token_data = response.json()

    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")

    # Verify Google identity.
    id_info = id_token.verify_oauth2_token(
        token_data["id_token"],
        google_requests.Request(),
        GOOGLE_CLIENT_ID,
    )

    google_sub = id_info["sub"]
    email = id_info["email"]

    async with AsyncSessionLocal() as session:

        result = await session.execute(
            select(UserDB).where(UserDB.google_sub == google_sub)
        )

        user = result.scalar_one_or_none()

        if not user:

            user = UserDB(
                google_sub=google_sub,
                email=email,
                name=id_info.get("name"),
                picture=id_info.get("picture"),
            )

            session.add(user)
            await session.flush()

        else:

            user.email = email
            user.name = id_info.get("name")
            user.picture = id_info.get("picture")

        result = await session.execute(
            select(OAuthTokenDB).where(OAuthTokenDB.user_id == user.id)
        )

        token = result.scalar_one_or_none()

        if not token:

            token = OAuthTokenDB(
                user_id=user.id,
                access_token=access_token,
                refresh_token=refresh_token,
                client_id=GOOGLE_CLIENT_ID,
                client_secret=GOOGLE_CLIENT_SECRET,
                scopes=SCOPES,
            )

            session.add(token)

        else:

            token.access_token = access_token

            if refresh_token:
                token.refresh_token = refresh_token

        await session.commit()

        request.session["user_id"] = user.id

    return {
        "authenticated": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
        },
    }


@router.post("/logout")
@limiter.limit("30/minute")
async def logout(request: Request):

    request.session.clear()

    return {"authenticated": False}
