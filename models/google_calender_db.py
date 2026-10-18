# models/google_calender_db.py

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, JSON
from sqlalchemy.types import Text
from models.base import Base


class CalendarEventDB(Base):
    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        index=True,
    )
    task_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        index=True,
        nullable=True,
    )
    request_text: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(String(255))
    start_time: Mapped[str] = mapped_column(String(100))
    end_time: Mapped[str] = mapped_column(String(100))
    attendees: Mapped[list] = mapped_column(JSON, default=list)
    meeting_link: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    google_event_id: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="scheduled")
    raw_function_call: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
    )
