"""
Unit tests for services/google_calender/service.py
"""

from datetime import datetime, timezone, timedelta
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from google.oauth2.credentials import Credentials
from models.auth_db import OAuthTokenDB
from schemas import (
    CalendarEventDateTime,
    ScheduleCalendarEventFunction,
    SendUpdatesOption,
)
from services.google_calender.service import (
    CACHE_TTL_SECONDS,
    DEFAULT_TOKEN_URI,
    GoogleCalendarService,
    get_gcal_service,
)


@pytest.mark.unit
class TestGoogleCalendarService:
    @pytest.fixture
    def mock_session(self):
        """Provide a mock AsyncSession."""
        session = AsyncMock()
        return session

    @pytest.fixture
    def service(self, mock_session):
        """Provide a GoogleCalendarService instance bound to a test user."""
        return GoogleCalendarService(user_id=42, session=mock_session)

    @pytest.fixture
    def sample_event_data(self):
        """Provide a valid ScheduleCalendarEventFunction payload."""
        return ScheduleCalendarEventFunction(
            summary="Strategic Architecture Review",
            description="Discussing multi-region failover and DLQ idempotency.",
            location="Room 404 / Google Meet",
            start=CalendarEventDateTime(
                date_time="2026-10-15T14:00:00Z", time_zone="UTC"
            ),
            end=CalendarEventDateTime(
                date_time="2026-10-15T15:00:00Z", time_zone="UTC"
            ),
            attendees=["lead.architect@example.com", "principal.eng@example.com"],
            send_updates=SendUpdatesOption.ALL,
        )

    # -------------------------------------------------------------------------
    # 1. Credential Fetching & Cache Handling
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    @patch("services.google_calender.service.get_cached_function_call")
    async def test_get_credentials_redis_cache_hit(
        self, mock_get_cache, service, mock_session
    ):
        """Verify cached credentials in Redis are parsed directly without touching the database."""
        expiry_dt = datetime.now(timezone.utc) + timedelta(hours=2)
        cached_payload = {
            "access_token": "ya29.cached_token_123",
            "refresh_token": "1//cached_refresh_456",
            "token_uri": DEFAULT_TOKEN_URI,
            "client_id": "test_client_id.apps.googleusercontent.com",
            "client_secret": "test_client_secret",
            "scopes": ["https://www.googleapis.com/auth/calendar"],
            "expiry": expiry_dt.isoformat(),
        }
        mock_get_cache.return_value = json.dumps(cached_payload)

        creds = await service._get_credentials()

        assert creds is not None
        assert isinstance(creds, Credentials)
        assert creds.token == "ya29.cached_token_123"
        assert creds.refresh_token == "1//cached_refresh_456"
        assert creds.client_id == "test_client_id.apps.googleusercontent.com"
        assert creds.scopes == ["https://www.googleapis.com/auth/calendar"]
        mock_session.execute.assert_not_called()

    @pytest.mark.anyio
    @patch("services.google_calender.service.get_cached_function_call")
    async def test_get_credentials_db_fallback_valid_token(
        self, mock_get_cache, service, mock_session
    ):
        """Verify valid token in database is returned when Redis cache misses."""
        mock_get_cache.return_value = None

        future_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        db_token = OAuthTokenDB(
            id=1,
            user_id=42,
            access_token="ya29.db_valid_token",
            refresh_token="1//db_refresh",
            token_uri=DEFAULT_TOKEN_URI,
            client_id="client_id_val",
            client_secret="client_secret_val",
            scopes=["https://www.googleapis.com/auth/calendar"],
            expiry=future_expiry,
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = db_token
        mock_session.execute.return_value = mock_result

        with patch.object(Credentials, "expired", False):
            creds = await service._get_credentials()

        assert creds is not None
        assert creds.token == "ya29.db_valid_token"
        assert creds.refresh_token == "1//db_refresh"
        mock_session.commit.assert_not_called()

    @pytest.mark.anyio
    @patch("services.google_calender.service.get_cached_function_call")
    async def test_get_credentials_db_record_not_found(
        self, mock_get_cache, service, mock_session
    ):
        """Verify None is returned when credentials exist neither in cache nor database."""
        mock_get_cache.return_value = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        creds = await service._get_credentials()
        assert creds is None

    # -------------------------------------------------------------------------
    # 2. Token Expiration & Refresh Logic
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    @patch("services.google_calender.service.get_shared_ledger")
    @patch("services.google_calender.service.set_cached_function_call")
    @patch("services.google_calender.service.get_cached_function_call")
    async def test_get_credentials_refresh_when_expired(
        self,
        mock_get_cache,
        mock_set_cache,
        mock_get_ledger,
        service,
        mock_session,
    ):
        """Verify expired database credentials trigger automated refresh, DB update, cache warming, and audit logging."""
        mock_get_cache.return_value = None

        past_expiry = datetime.now(timezone.utc) - timedelta(minutes=10)
        db_token = OAuthTokenDB(
            id=1,
            user_id=42,
            access_token="ya29.old_expired_token",
            refresh_token="1//valid_refresh_token",
            token_uri=DEFAULT_TOKEN_URI,
            client_id="client_id_val",
            client_secret="client_secret_val",
            scopes=["https://www.googleapis.com/auth/calendar"],
            expiry=past_expiry,
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = db_token
        mock_session.execute.return_value = mock_result

        refreshed_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        mock_ledger = MagicMock()
        mock_get_ledger.return_value = mock_ledger

        # Patch Credentials methods to simulate successful refresh
        with patch.object(Credentials, "expired", True), patch.object(
            Credentials, "refresh"
        ) as mock_refresh:

            def refresh_side_effect(request):
                # Credentials.refresh mutates self.token and self.expiry
                pass

            mock_refresh.side_effect = refresh_side_effect

            with patch(
                "services.google_calender.service.Credentials"
            ) as mock_creds_cls:
                mock_cred_instance = MagicMock(spec=Credentials)
                mock_cred_instance.expired = True
                mock_cred_instance.refresh_token = "1//valid_refresh_token"
                mock_cred_instance.token = "ya29.brand_new_refreshed_token"
                mock_cred_instance.expiry = refreshed_expiry
                mock_cred_instance.token_uri = DEFAULT_TOKEN_URI
                mock_cred_instance.client_id = "client_id_val"
                mock_cred_instance.client_secret = "client_secret_val"
                mock_cred_instance.scopes = ["https://www.googleapis.com/auth/calendar"]
                mock_creds_cls.return_value = mock_cred_instance

                creds = await service._get_credentials()

                assert creds == mock_cred_instance
                mock_cred_instance.refresh.assert_called_once()
                assert db_token.access_token == "ya29.brand_new_refreshed_token"
                assert db_token.expiry == refreshed_expiry
                mock_session.commit.assert_awaited_once()

                # Verify Redis cache update
                mock_set_cache.assert_awaited_once()
                cache_args = mock_set_cache.await_args[1]
                assert cache_args["cache_key"] == "google_oauth_token:42"
                assert (
                    cache_args["data"]["access_token"]
                    == "ya29.brand_new_refreshed_token"
                )
                assert cache_args["expire_seconds"] == CACHE_TTL_SECONDS

                # Verify audit ledger append
                mock_ledger.append.assert_called_once_with(
                    "credentials_refreshed",
                    {
                        "user_id": 42,
                        "expiry": refreshed_expiry.isoformat(),
                    },
                    trace_id="42",
                )

    # -------------------------------------------------------------------------
    # 3. Event Creation with Google Meet & Error Handling
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    @patch("services.google_calender.service.get_shared_ledger")
    @patch("services.google_calender.service.build")
    async def test_create_event_with_meet_success(
        self, mock_build, mock_get_ledger, service, sample_event_data
    ):
        """Verify event creation builds correct payload with hangoutsMeet and records success in ledger."""
        mock_creds = MagicMock(spec=Credentials)
        service._get_credentials = AsyncMock(return_value=mock_creds)

        mock_calendar_client = MagicMock()
        mock_events_resource = MagicMock()
        mock_insert_request = MagicMock()

        api_response = {
            "id": "event_gcal_9999",
            "htmlLink": "https://calendar.google.com/calendar/event?eid=abc",
            "status": "confirmed",
            "conferenceData": {
                "entryPoints": [
                    {
                        "entryPointType": "video",
                        "uri": "https://meet.google.com/xyz-abcd-efg",
                    }
                ]
            },
        }

        mock_insert_request.execute.return_value = api_response
        mock_events_resource.insert.return_value = mock_insert_request
        mock_calendar_client.events.return_value = mock_events_resource
        mock_build.return_value = mock_calendar_client

        mock_ledger = MagicMock()
        mock_get_ledger.return_value = mock_ledger

        result = await service.create_event_with_meet(sample_event_data)

        assert result["event_id"] == "event_gcal_9999"
        assert (
            result["html_link"] == "https://calendar.google.com/calendar/event?eid=abc"
        )
        assert result["meeting_link"] == "https://meet.google.com/xyz-abcd-efg"
        assert result["status"] == "confirmed"

        # Check API insert parameters
        mock_events_resource.insert.assert_called_once()
        call_kwargs = mock_events_resource.insert.call_args[1]
        assert call_kwargs["calendarId"] == service.calendar_id
        assert call_kwargs["conferenceDataVersion"] == 1
        assert call_kwargs["sendUpdates"] == "all"
        assert call_kwargs["body"]["summary"] == "Strategic Architecture Review"
        assert len(call_kwargs["body"]["attendees"]) == 2
        assert (
            call_kwargs["body"]["conferenceData"]["createRequest"][
                "conferenceSolutionKey"
            ]["type"]
            == "hangoutsMeet"
        )

        # Check ledger recorded success
        mock_ledger.append.assert_called_once_with(
            "google_calendar_created",
            {
                "user_id": 42,
                "event_id": "event_gcal_9999",
                "status": "confirmed",
            },
            trace_id="42",
        )

    @pytest.mark.anyio
    @patch("services.google_calender.service.get_shared_ledger")
    @patch("services.google_calender.service.build")
    async def test_create_event_with_meet_fallback_hangouts_link(
        self, mock_build, mock_get_ledger, service, sample_event_data
    ):
        """Verify fallback to hangoutsLink when conferenceData entryPoints has no video type."""
        mock_creds = MagicMock(spec=Credentials)
        service._get_credentials = AsyncMock(return_value=mock_creds)

        mock_calendar_client = MagicMock()
        mock_insert_request = MagicMock()
        mock_insert_request.execute.return_value = {
            "id": "event_hangouts_fallback",
            "htmlLink": "https://calendar.google.com/event",
            "status": "confirmed",
            "conferenceData": {"entryPoints": []},
            "hangoutsLink": "https://plus.google.com/hangouts/_/example.com/fallback",
        }
        mock_calendar_client.events().insert.return_value = mock_insert_request
        mock_build.return_value = mock_calendar_client

        result = await service.create_event_with_meet(sample_event_data)
        assert (
            result["meeting_link"]
            == "https://plus.google.com/hangouts/_/example.com/fallback"
        )

    @pytest.mark.anyio
    async def test_create_event_with_meet_unauthenticated_raises_error(
        self, service, sample_event_data
    ):
        """Verify RuntimeError is raised when credentials cannot be resolved."""
        service._get_credentials = AsyncMock(return_value=None)

        with pytest.raises(
            RuntimeError, match="Google Calendar authentication required"
        ):
            await service.create_event_with_meet(sample_event_data)

    @pytest.mark.anyio
    @patch("services.google_calender.service.get_shared_ledger")
    @patch("services.google_calender.service.build")
    async def test_create_event_with_meet_api_failure_audits_and_re_raises(
        self, mock_build, mock_get_ledger, service, sample_event_data
    ):
        """Verify Google API insert errors are recorded in the audit ledger and re-raised."""
        mock_creds = MagicMock(spec=Credentials)
        service._get_credentials = AsyncMock(return_value=mock_creds)

        mock_calendar_client = MagicMock()
        mock_insert_request = MagicMock()
        mock_insert_request.execute.side_effect = RuntimeError(
            "Backend Google API 500: Server Error"
        )
        mock_calendar_client.events().insert.return_value = mock_insert_request
        mock_build.return_value = mock_calendar_client

        mock_ledger = MagicMock()
        mock_get_ledger.return_value = mock_ledger

        with pytest.raises(RuntimeError, match="Backend Google API 500: Server Error"):
            await service.create_event_with_meet(sample_event_data)

        # Check ledger recorded failure event
        mock_ledger.append.assert_called_once_with(
            "google_calendar_create_failed",
            {
                "user_id": 42,
                "error": "Backend Google API 500: Server Error",
            },
            trace_id="42",
        )

    # -------------------------------------------------------------------------
    # 4. Factory Helper
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_get_gcal_service_factory(self, mock_session):
        """Verify factory helper correctly returns configured GoogleCalendarService instance."""
        inst = await get_gcal_service(user_id=101, session=mock_session)
        assert isinstance(inst, GoogleCalendarService)
        assert inst.user_id == 101
        assert inst.session == mock_session
