"""Integration tests for services/google_calender/service.py."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from models.auth_db import OAuthTokenDB
from schemas import (
    ScheduleCalendarEventFunction,
    CalendarEventDateTime,
    SendUpdatesOption,
)
from services.google_calender.service import GoogleCalendarService


@pytest.mark.integration
@pytest.mark.anyio
class TestGoogleCalendarServiceIntegration:
    @pytest.fixture
    def mock_session(self):
        session = AsyncMock()
        return session

    @pytest.fixture
    def sample_event(self):
        return ScheduleCalendarEventFunction(
            summary="Product Architecture Review",
            description="Deep dive on caching & database schemas",
            location="Room 404 / Meet",
            start=CalendarEventDateTime(
                date_time="2026-10-25T14:00:00Z", time_zone="UTC"
            ),
            end=CalendarEventDateTime(
                date_time="2026-10-25T15:00:00Z", time_zone="UTC"
            ),
            attendees=["eng-lead@example.com", "architect@example.com"],
            send_updates=SendUpdatesOption.ALL,
        )

    async def test_get_credentials_cache_hit(self, mock_session):
        """Verify cached credentials bypass database query."""
        service = GoogleCalendarService(user_id=42, session=mock_session)

        cached_data = {
            "access_token": "cached_access_token_123",
            "refresh_token": "cached_refresh_token_456",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "google_client_id.apps.googleusercontent.com",
            "client_secret": "client_secret_xyz",
            "scopes": ["https://www.googleapis.com/auth/calendar"],
            "expiry": "2026-12-31T00:00:00Z",
        }

        with patch(
            "services.google_calender.service.get_cached_function_call",
            new_callable=AsyncMock,
            return_value=json.dumps(cached_data),
        ):
            creds = await service._get_credentials()

            assert creds is not None
            assert creds.token == "cached_access_token_123"
            mock_session.execute.assert_not_called()

    async def test_get_credentials_db_fallback_and_token_refresh(self, mock_session):
        """Verify expired token from database triggers refresh and updates Redis cache."""
        service = GoogleCalendarService(user_id=99, session=mock_session)

        # Token expired 1 hour ago
        expired_dt = datetime.now(timezone.utc) - timedelta(hours=1)
        token_record = OAuthTokenDB(
            id=1,
            user_id=99,
            access_token="expired_token_789",
            refresh_token="valid_refresh_token_012",
            expiry=expired_dt,
            token_uri="https://oauth2.googleapis.com/token",
            client_id="client_id_123",
            client_secret="client_secret_456",
            scopes=["https://www.googleapis.com/auth/calendar"],
        )

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = token_record
        mock_session.execute.return_value = result_mock

        with patch(
            "services.google_calender.service.get_cached_function_call",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "services.google_calender.service.Credentials"
        ) as mock_creds_class, patch(
            "services.google_calender.service.set_cached_function_call",
            new_callable=AsyncMock,
        ) as mock_set_cache:
            mock_creds_instance = MagicMock()
            mock_creds_instance.expired = True
            mock_creds_instance.refresh_token = "valid_refresh_token_012"
            mock_creds_instance.token = "refreshed_token_abc"
            mock_creds_instance.expiry = datetime.now(timezone.utc) + timedelta(hours=1)
            mock_creds_instance.token_uri = "https://oauth2.googleapis.com/token"
            mock_creds_instance.client_id = "client_id_123"
            mock_creds_instance.client_secret = "client_secret_456"
            mock_creds_instance.scopes = ["https://www.googleapis.com/auth/calendar"]
            mock_creds_class.return_value = mock_creds_instance

            creds = await service._get_credentials()

            assert creds is mock_creds_instance
            mock_creds_instance.refresh.assert_called_once()
            mock_session.commit.assert_awaited_once()
            mock_set_cache.assert_called_once()

    async def test_create_event_with_meet_integration(self, mock_session, sample_event):
        """Verify end-to-end event creation with Google Meet link generation and audit trail."""
        service = GoogleCalendarService(user_id=42, session=mock_session)

        mock_creds = MagicMock()
        mock_creds.valid = True

        mock_events_resource = MagicMock()
        mock_insert = MagicMock()
        mock_insert.execute.return_value = {
            "id": "gcal_event_888",
            "summary": "Product Architecture Review",
            "htmlLink": "https://calendar.google.com/event?eid=888",
            "hangoutsLink": "https://meet.google.com/abc-defg-hij",
            "conferenceData": {
                "entryPoints": [
                    {
                        "entryPointType": "video",
                        "uri": "https://meet.google.com/abc-defg-hij",
                    }
                ]
            },
            "status": "confirmed",
        }
        mock_events_resource.insert.return_value = mock_insert

        mock_gcal_client = MagicMock()
        mock_gcal_client.events.return_value = mock_events_resource

        with patch.object(
            service, "_get_credentials", new_callable=AsyncMock, return_value=mock_creds
        ), patch(
            "services.google_calender.service.build", return_value=mock_gcal_client
        ), patch(
            "services.google_calender.service.get_shared_ledger"
        ) as mock_get_ledger:

            mock_ledger = MagicMock()
            mock_get_ledger.return_value = mock_ledger

            result = await service.create_event_with_meet(sample_event)

            assert result["event_id"] == "gcal_event_888"
            assert result["meeting_link"] == "https://meet.google.com/abc-defg-hij"
            assert result["status"] == "confirmed"

            # Verify Google API build & insert payload
            mock_events_resource.insert.assert_called_once()
            call_kwargs = mock_events_resource.insert.call_args.kwargs
            assert call_kwargs["calendarId"] == service.calendar_id
            assert call_kwargs["conferenceDataVersion"] == 1
            assert call_kwargs["body"]["summary"] == "Product Architecture Review"
            assert (
                call_kwargs["body"]["conferenceData"]["createRequest"][
                    "conferenceSolutionKey"
                ]["type"]
                == "hangoutsMeet"
            )

            # Audit ledger records success
            mock_ledger.append.assert_called_once()
            args = mock_ledger.append.call_args[0]
            assert args[0] == "google_calendar_created"
            assert args[1]["event_id"] == "gcal_event_888"

    async def test_create_event_unauthenticated_raises(
        self, mock_session, sample_event
    ):
        """Verify unauthenticated user raises RuntimeError."""
        service = GoogleCalendarService(user_id=101, session=mock_session)

        with patch.object(
            service, "_get_credentials", new_callable=AsyncMock, return_value=None
        ):
            with pytest.raises(RuntimeError) as exc_info:
                await service.create_event_with_meet(sample_event)

            assert "authentication required" in str(exc_info.value).lower()
