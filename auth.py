# auth.py
# This file contains the authentication logic for the FastAPI application.
# It includes a dependency function to retrieve the current authenticated user from the session.

from fastapi import HTTPException, Request
from sqlalchemy import select

from config.database import AsyncSessionLocal
from config.settings import settings
from models.user_db import UserDB
from utilities.security import build_tenant_context


async def get_current_user(
    request: Request,
) -> UserDB:

    user_id = request.session.get("user_id")

    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
        )

    async with AsyncSessionLocal() as session:

        result = await session.execute(select(UserDB).where(UserDB.id == user_id))

        user = result.scalar_one_or_none()

        if not user:
            raise HTTPException(
                status_code=401,
                detail="User no longer exists",
            )

        tenant_token = build_tenant_context(
            tenant_id=user.id,
            scopes=["calendar:read", "calendar:write", "dlq:replay"],
            operation="auth",
            entity_key=f"user:{user.id}",
            bounds={
                "user_id": user.id,
                "allowed_operations": ["schedule", "replay", "audit"],
            },
        )
        request.session["tenant_context"] = tenant_token
        request.state.tenant_claims = {
            "tenant_id": str(user.id),
            "user_id": user.id,
            "scopes": ["calendar:read", "calendar:write", "dlq:replay"],
            "entity_key": f"user:{user.id}",
        }
        request.state.tenant_id = user.id
        request.state.user_id = user.id

        return user


async def get_tenant_claims(request: Request):
    token = request.session.get("tenant_context")
    if not token:
        raise HTTPException(status_code=401, detail="Tenant claims missing")

    from utilities.security import verify_tenant_context

    claims = verify_tenant_context(token)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid tenant claims")

    return claims
