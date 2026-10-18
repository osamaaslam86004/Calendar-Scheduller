# models/auth_db.py

import os
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, DateTime, JSON, Index
from models.base import Base
from utilities.security import EncryptedString


class OAuthTokenDB(Base):
    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(index=True)
    access_token: Mapped[str] = mapped_column(EncryptedString)
    refresh_token: Mapped[Optional[str]] = mapped_column(EncryptedString, nullable=True)
    token_uri: Mapped[str] = mapped_column(
        String(500),
        default="https://oauth2.googleapis.com/token",
    )
    client_id: Mapped[str] = mapped_column(EncryptedString)
    client_secret: Mapped[str] = mapped_column(EncryptedString)
    scopes: Mapped[list] = mapped_column(JSON)
    expiry: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        Index(
            "ix_oauth_user_id",
            "user_id",
            unique=True,
        ),
    )
