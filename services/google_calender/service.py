# gcloud_service.py
# That gives you actual multi-tenancy instead of a database column pretending to be multi-tenancy.

import json
import os
import uuid
import logging
import asyncio
from datetime import datetime
from typing import Any, Dict, Optional

from telemetry.manager import get_shared_ledger
from config.settings import settings

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from utilities.cache import get_cached_function_call, set_cached_function_call
from models.auth_db import OAuthTokenDB

# initialize module logger
logger = logging.getLogger(__name__)


CACHE_TTL_SECONDS = 3600  # 1 hour cache window
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"


class GoogleCalendarService:

    def __init__(self, user_id: int, session: AsyncSession):

        self.user_id = user_id
        self.session = session
        self.calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")

    async def _get_credentials(self) -> Optional[Credentials]:

        cache_key = f"google_oauth_token:{self.user_id}"

        # Step 1: Check Redis Cache
        cached_token_data = await get_cached_function_call(cache_key)
        token_dict = None

        if cached_token_data:
            token_dict = json.loads(cached_token_data)

            expiry_raw = token_dict.get("expiry")
            expiry_dt = datetime.fromisoformat(expiry_raw) if expiry_raw else None

            credentials = Credentials(
                token=token_dict.get("access_token"),
                refresh_token=token_dict.get("refresh_token"),
                token_uri=token_dict.get("token_uri"),
                client_id=token_dict.get("client_id"),
                client_secret=token_dict.get("client_secret"),
                scopes=token_dict.get("scopes"),
                expiry=expiry_dt,
            )
            return credentials

        # Step 2: Fetch from Database
        result = await self.session.execute(
            select(OAuthTokenDB).where(OAuthTokenDB.user_id == self.user_id)
        )

        token = result.scalar_one_or_none()

        if not token:
            return None

        token_uri = getattr(token, "token_uri", None) or DEFAULT_TOKEN_URI

        credentials = Credentials(
            token=token.access_token,
            refresh_token=(token.refresh_token),
            token_uri=(token.token_uri),
            client_id=(token.client_id),
            client_secret=(token.client_secret),
            scopes=token.scopes,
            expiry=token.expiry,
        )

        # Refresh the token if it's expired
        if credentials.expired and credentials.refresh_token:

            credentials.refresh(Request())

            token.access_token = credentials.token

            # Update the expiry in the database
            token.expiry = credentials.expiry

            await self.session.commit()

            # Build clean serializable dict for Redis Cache
            cache_payload = {
                "access_token": credentials.token,
                "refresh_token": credentials.refresh_token,
                "token_uri": credentials.token_uri,
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
                "scopes": credentials.scopes,
                "expiry": (
                    credentials.expiry.isoformat() if credentials.expiry else None
                ),
            }

            # Write back to Redis Cache
            await set_cached_function_call(
                cache_key=cache_key,
                data=cache_payload,
                expire_seconds=CACHE_TTL_SECONDS,
            )

            # Append non-sensitive credential refresh metadata to audit ledger
            try:
                ledger = get_shared_ledger()
                ledger.append(
                    "credentials_refreshed",
                    {
                        "user_id": self.user_id,
                        "expiry": (
                            credentials.expiry.isoformat()
                            if credentials.expiry
                            else None
                        ),
                    },
                    trace_id=str(self.user_id),
                )
            except Exception:
                logger.exception("Failed to record credential refresh in audit ledger")

        return credentials

    async def create_event_with_meet(self, event_data) -> Dict[str, Any]:

        credentials = await self._get_credentials()

        if not credentials:

            raise RuntimeError("Google Calendar authentication " "required")

        service = build(
            "calendar",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

        event_body = {
            "summary": event_data.summary,
            "description": event_data.description,
            "location": event_data.location,
            "start": {
                "dateTime": event_data.start.date_time,
                "timeZone": event_data.start.time_zone,
            },
            "end": {
                "dateTime": event_data.end.date_time,
                "timeZone": event_data.end.time_zone,
            },
            "attendees": [{"email": str(email)} for email in event_data.attendees],
            "conferenceData": {
                "createRequest": {
                    "requestId": uuid.uuid4().hex,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
        }

        try:
            created_event = (
                service.events()
                .insert(
                    calendarId=self.calendar_id,
                    body=event_body,
                    conferenceDataVersion=1,
                    sendUpdates=(event_data.send_updates.value),
                )
                .execute()
            )
        except Exception as exc:
            logger.exception("Calendar create failed: %s", exc)
            try:
                ledger = get_shared_ledger()
                ledger.append(
                    "google_calendar_create_failed",
                    {"user_id": self.user_id, "error": str(exc)},
                    trace_id=str(self.user_id),
                )
            except Exception:
                logger.exception(
                    "Failed to record calendar create failure in audit ledger"
                )
            raise

        conference_data = created_event.get(
            "conferenceData",
            {},
        )

        entry_points = conference_data.get(
            "entryPoints",
            [],
        )

        meeting_link = next(
            (
                ep.get("uri")
                for ep in entry_points
                if ep.get("entryPointType") == "video"
            ),
            created_event.get("hangoutsLink"),
        )

        # Record successful creation in ledger
        try:
            ledger = get_shared_ledger()
            ledger.append(
                "google_calendar_created",
                {
                    "user_id": self.user_id,
                    "event_id": created_event.get("id"),
                    "status": created_event.get("status"),
                },
                trace_id=str(self.user_id),
            )
        except Exception:
            logger.exception("Failed to record calendar create success in audit ledger")

        return {
            "event_id": created_event.get("id"),
            "html_link": created_event.get("htmlLink"),
            "meeting_link": meeting_link,
            "status": created_event.get("status"),
        }


async def get_gcal_service(
    user_id: int, session: AsyncSession
) -> GoogleCalendarService:

    return GoogleCalendarService(user_id=user_id, session=session)
