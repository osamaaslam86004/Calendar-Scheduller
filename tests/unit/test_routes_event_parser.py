"""
Unit tests for routes/event_parser.py
"""

from unittest.mock import MagicMock, patch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.google_calender_db import CalendarEventDB
from routes.event_parser import event_parser_router
from schemas import ScheduleCalendarEventFunction


@pytest.mark.unit
class TestEventParser:
    @pytest.fixture
    def app(self):
        """Build test application mounting event_parser_router."""
        app = FastAPI()
        app.include_router(event_parser_router)
        return app

    @pytest.fixture
    def client(self, app):
        """Provide TestClient for event parser endpoint."""
        return TestClient(app)

    @pytest.fixture
    def sample_function_dict(self):
        """Provide a valid calendar function dictionary."""
        return {
            "summary": "Quarterly Planning",
            "start": {"date_time": "2026-10-20T14:00:00Z"},
            "end": {"date_time": "2026-10-20T15:30:00Z"},
            "attendees": ["alice@example.com", "bob@example.com"],
        }

    @patch("routes.event_parser.CalendarEventDB")
    @patch("routes.event_parser.Session")
    @patch("routes.event_parser.set_cached_function_call", new_callable=MagicMock)
    @patch("routes.event_parser.get_calendar_engine")
    @patch("routes.event_parser.get_cached_function_call", new_callable=MagicMock)
    def test_parse_calendar_event_cache_miss_persists_and_returns(
        self,
        mock_get_cached,
        mock_get_engine,
        mock_set_cached,
        mock_session_cls,
        mock_event_db_cls,
        client,
        sample_function_dict,
    ):
        """Verify cache miss extracts event via calendar engine, caches, persists to DB, and returns response."""
        mock_get_cached.return_value = None

        mock_engine = MagicMock()
        mock_func_call = ScheduleCalendarEventFunction.model_validate(
            sample_function_dict
        )
        mock_engine.extract_calendar_function.return_value = mock_func_call
        mock_get_engine.return_value = mock_engine

        mock_db_session = MagicMock()
        mock_session_cls.return_value.__enter__.return_value = mock_db_session

        mock_record = MagicMock(id=88)
        mock_event_db_cls.return_value = mock_record

        payload = {"request_text": "Schedule quarterly planning on Oct 20 at 2pm"}
        response = client.post("/api/v1/parse-event", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 88
        assert data["cached"] is False
        assert data["function_call"]["summary"] == "Quarterly Planning"
        assert len(data["function_call"]["attendees"]) == 2

        assert mock_set_cached.called
        assert mock_db_session.add.called
        assert mock_db_session.commit.called

    @patch("routes.event_parser.get_calendar_engine")
    @patch("routes.event_parser.get_cached_function_call", new_callable=MagicMock)
    def test_parse_calendar_event_cache_hit_returns_cached_result(
        self, mock_get_cached, mock_get_engine, client, sample_function_dict
    ):
        """Verify cache hit bypasses engine extraction and database persistence returning id=0 and cached=True."""
        mock_get_cached.return_value = sample_function_dict

        payload = {"request_text": "Schedule quarterly planning on Oct 20 at 2pm"}
        response = client.post("/api/v1/parse-event", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 0
        assert data["cached"] is True
        assert data["function_call"]["summary"] == "Quarterly Planning"

        mock_get_engine.assert_not_called()

    def test_parse_calendar_event_missing_payload(self, client):
        """Verify validation error with HTTP 422 when required request_text field is missing."""
        response = client.post("/api/v1/parse-event", json={})
        assert response.status_code == 422

    def test_parse_calendar_event_invalid_json_type(self, client):
        """Verify validation error when payload is not a valid JSON object."""
        response = client.post(
            "/api/v1/parse-event",
            content="not a json payload",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422

    @patch("routes.event_parser.get_calendar_engine")
    @patch("routes.event_parser.get_cached_function_call", new_callable=MagicMock)
    def test_parse_calendar_event_engine_failure_propagates_error(
        self, mock_get_cached, mock_get_engine, client
    ):
        """Verify error is raised when engine function extraction fails."""
        mock_get_cached.return_value = None

        mock_engine = MagicMock()
        mock_engine.extract_calendar_function.side_effect = RuntimeError(
            "LLM extraction parsing error"
        )
        mock_get_engine.return_value = mock_engine

        with pytest.raises(RuntimeError, match="LLM extraction parsing error"):
            client.post(
                "/api/v1/parse-event",
                json={"request_text": "Unparseable prompt text"},
            )
