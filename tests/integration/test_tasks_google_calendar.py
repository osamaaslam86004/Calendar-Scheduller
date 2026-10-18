"""Integration tests for tasks/google_calender_meeting.py."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from celery.exceptions import MaxRetriesExceededError
from googleapiclient.errors import HttpError
from sqlalchemy.exc import SQLAlchemyError

from schemas import (
    ScheduleCalendarEventFunction,
    CalendarEventDateTime,
    SendUpdatesOption,
)
from tasks.google_calender_meeting import (
    _execute_schedule,
    execute_calendar_schedule_task,
)


@pytest.mark.integration
@pytest.mark.anyio
class TestTasksGoogleCalendarIntegration:
    @pytest.fixture
    def sample_func_call(self):
        return ScheduleCalendarEventFunction(
            summary="Engineering Alignment Sync",
            description="Discussion on queue latency and worker scaling",
            location="Meet",
            start=CalendarEventDateTime(
                date_time="2026-10-28T16:00:00Z", time_zone="UTC"
            ),
            end=CalendarEventDateTime(
                date_time="2026-10-28T17:00:00Z", time_zone="UTC"
            ),
            attendees=["lead@corp.com"],
            send_updates=SendUpdatesOption.ALL,
        )

    @pytest.fixture
    def sample_meta(self):
        return {
            "provider": "google",
            "model_name": "gemini-2.5-flash-lite",
            "prompt_tokens": 120,
            "completion_tokens": 60,
            "cached_tokens": 30,
            "raw_meta": {"finish_reason": "STOP"},
        }

    async def test_execute_schedule_full_flow(self, sample_func_call, sample_meta):
        """Verify full flow: LLM extraction -> usage tracking -> calendar create -> DB save -> audit logs."""
        mock_engine = MagicMock()
        mock_engine.dispose = AsyncMock()

        mock_session = AsyncMock()
        mock_sessionmaker = MagicMock()
        mock_sessionmaker.return_value.__aenter__.return_value = mock_session
        mock_sessionmaker.return_value.__aexit__.return_value = None

        calendar_engine_mock = MagicMock()
        calendar_engine_mock.extract_calendar_function.return_value = (
            sample_func_call,
            sample_meta,
        )

        mock_gcal_service = MagicMock()
        mock_gcal_service.create_event_with_meet = AsyncMock(
            return_value={
                "event_id": "gcal_evt_12345",
                "meeting_link": "https://meet.google.com/abc-xyz-123",
                "html_link": "https://calendar.google.com/event?eid=12345",
                "status": "confirmed",
            }
        )

        mock_ledger = MagicMock()
        events_published = []

        def mock_publish(channel, event):
            events_published.append(event)

        with patch(
            "tasks.google_calender_meeting.get_task_sessionmaker",
            return_value=(mock_engine, mock_sessionmaker),
        ), patch(
            "tasks.google_calender_meeting.get_calendar_engine",
            return_value=calendar_engine_mock,
        ), patch(
            "tasks.google_calender_meeting.get_gcal_service",
            new_callable=AsyncMock,
            return_value=mock_gcal_service,
        ), patch(
            "tasks.google_calender_meeting.UsageTrackerService.log_usage",
            new_callable=AsyncMock,
        ) as mock_log_usage, patch(
            "tasks.google_calender_meeting.get_shared_ledger",
            return_value=mock_ledger,
        ), patch(
            "tasks.google_calender_meeting.publish_task_event",
            side_effect=mock_publish,
        ), patch(
            "tasks.google_calender_meeting.close_redis",
            new_callable=AsyncMock,
        ):
            result = await _execute_schedule(
                task_id="celery_task_001",
                user_id=42,
                request_text="Schedule engineering sync next Wednesday at 4pm",
            )

            assert result["meeting_link"] == "https://meet.google.com/abc-xyz-123"
            assert result["google_calendar_event"]["event_id"] == "gcal_evt_12345"

            # Check DB record creation
            mock_session.add.assert_called_once()
            db_record = mock_session.add.call_args[0][0]
            assert db_record.user_id == 42
            assert db_record.task_id == "celery_task_001"
            assert db_record.summary == "Engineering Alignment Sync"
            mock_session.commit.assert_awaited()

            # Check UsageTracker logged LLM costs
            mock_log_usage.assert_awaited_once()

            # Check events published in lifecycle sequence
            event_types = [e["type"] for e in events_published]
            assert "TASK_STARTED" in event_types
            assert "LLM_STARTED" in event_types
            assert "LLM_COMPLETED" in event_types
            assert "GOOGLE_CALENDAR_STARTED" in event_types
            assert "TASK_COMPLETED" in event_types

            # Verify audit ledger calls
            assert mock_ledger.append.call_count >= 3

    def test_execute_calendar_schedule_task_db_error_handling(self):
        """Verify SQLAlchemyError is caught and handled gracefully returning error dictionary."""
        mock_self = MagicMock()
        mock_self.request.id = "celery_db_fail_task"

        db_error = SQLAlchemyError("PostgreSQL pool connection timeout")

        with patch(
            "tasks.google_calender_meeting._execute_schedule",
            side_effect=db_error,
        ), patch("tasks.google_calender_meeting.publish_task_event") as mock_publish:
            res = execute_calendar_schedule_task.run(
                mock_self,
                10,
                "Test meeting",
            )

            assert res["status"] == "error"
            assert "Database error occurred" in res["message"]
            mock_publish.assert_called_once()
            args = mock_publish.call_args[0]
            assert args[0] == "celery_db_fail_task"
            assert args[1]["type"] == "TASK_FAILED_DB_ERROR"

    def test_execute_calendar_schedule_task_http_transient_retry(self):
        """Verify transient HttpError (e.g. 503 Service Unavailable) triggers self.retry."""
        mock_self = MagicMock()
        mock_self.request.id = "celery_http_retry_task"
        mock_self.request.retries = 1
        mock_self.retry.side_effect = Exception("Celery retry triggered")

        resp_mock = MagicMock()
        resp_mock.status = 503
        http_transient_error = HttpError(resp=resp_mock, content=b"Backend unavailable")

        with patch(
            "tasks.google_calender_meeting._execute_schedule",
            side_effect=http_transient_error,
        ):
            with pytest.raises(Exception) as exc_info:
                execute_calendar_schedule_task.run(
                    mock_self,
                    10,
                    "Meeting",
                )

            assert "Celery retry triggered" in str(exc_info.value)
            mock_self.retry.assert_called_once()
            assert mock_self.retry.call_args.kwargs["countdown"] == 2  # 2 ** 1

    def test_execute_calendar_schedule_task_timeout_dlq_escalation(self):
        """Verify that when TimeoutError exceeds max retries, it pushes to Dead Letter Queue."""
        mock_self = MagicMock()
        mock_self.request.id = "celery_timeout_dlq_task"
        mock_self.request.retries = 5
        mock_self.retry.side_effect = MaxRetriesExceededError("Max retries exceeded")

        with patch(
            "tasks.google_calender_meeting._execute_schedule",
            side_effect=TimeoutError("Downstream gateway timeout"),
        ), patch("tasks.google_calender_meeting.push_dead_letter") as mock_push_dlq:
            with pytest.raises(MaxRetriesExceededError):
                execute_calendar_schedule_task.run(
                    mock_self,
                    15,
                    "High priority executive sync",
                )

            mock_push_dlq.assert_called_once_with(
                task_id="celery_timeout_dlq_task",
                user_id=15,
                request_text="High priority executive sync",
                error="Downstream gateway timeout",
            )
