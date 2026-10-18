# tasks.py

import asyncio
import time
import logging
import os

from outlines.templates import Template
from celery.exceptions import MaxRetriesExceededError
from celery_app import celery_app
from googleapiclient.errors import HttpError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.exc import SQLAlchemyError
from dlq import push_dead_letter
from telemetry.manager import get_shared_ledger

from config.cache import close_redis
from config.settings import settings
from models.google_calender_db import CalendarEventDB
from services.usage_tracker.service import UsageTrackerService
from engine import get_calendar_engine
from tasks.events import publish_task_event
from services.google_calender.service import get_gcal_service
from telemetry.manager import get_shared_ledger

# initialize module logger
logger = logging.getLogger(__name__)

TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}

STATIC_SYSTEM_INSTRUCTIONS = Template.from_file("utilities/templates/system_prompt.txt")

# 1. Reuse central settings to resolve database URL and async driver
ASYNC_DB_URL = str(settings.db.async_url)


def get_task_sessionmaker() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """
    Creates an async engine and sessionmaker per task execution using NullPool.
    Prevents cross-event-loop connection contamination when using asyncio.run().
    """

    _worker_engine = create_async_engine(ASYNC_DB_URL, poolclass=NullPool)
    _worker_sessionmaker = async_sessionmaker(
        _worker_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return _worker_engine, _worker_sessionmaker


async def _execute_schedule(
    task_id: str,
    user_id: int,
    request_text: str,
):
    """
    Execute the calendar scheduling task using the cached worker sessionmaker

    Prevents Event Loop Contamination: Calling asyncio.run() creates a clean event
    loop per task execution. Because pool_pre_ping=True is enabled, connections drawn
    from the pool inside asyncio.run() validate automatically without event loop attachment
    conflicts (It means, the database session's async loop remain bind to this celery task event
    loop to prevent error:
    `attached to a different loop`).
    """

    _worker_engine, _worker_sessionmaker = get_task_sessionmaker()

    publish_task_event(
        task_id,
        {"type": "TASK_STARTED", "task_id": task_id, "user_id": user_id},
    )

    # record task start in shared ledger
    try:
        ledger = get_shared_ledger()
        ledger.append(
            "task_started",
            {"task_id": task_id, "user_id": user_id, "request_text": request_text},
            trace_id=task_id,
        )
    except Exception:
        logger.exception("Failed to append task_started to audit ledger")

    publish_task_event(
        task_id,
        {"type": "LLM_STARTED", "task_id": task_id, "user_id": user_id},
    )

    engine_instance = get_calendar_engine()

    start_time = time.perf_counter()
    # Extract schema and raw token metadata from engine
    func_call, meta = engine_instance.extract_calendar_function(request_text)
    latency_ms = (time.perf_counter() - start_time) * 1000

    publish_task_event(
        task_id,
        {"type": "LLM_COMPLETED", "task_id": task_id, "user_id": user_id},
    )

    try:
        # Fire-and-forget logging via UsageTrackerService
        async with _worker_sessionmaker() as session:
            log_entry = await UsageTrackerService.log_usage(
                session=session,
                provider=meta["provider"],
                model_name=meta["model_name"],
                query_text=request_text,
                system_instruction=str(STATIC_SYSTEM_INSTRUCTIONS),
                prompt_tokens=meta["prompt_tokens"],
                completion_tokens=meta["completion_tokens"],
                cached_tokens=meta["cached_tokens"],
                execution_time_ms=latency_ms,
                user_id=user_id,
                raw_meta=meta["raw_meta"],
            )
    except Exception as exc:
        # Properly logs message and full exception traceback via JSONFormatter
        logger.exception("Failed to commit usage log record: %s", exc)

    publish_task_event(
        task_id,
        {"type": "GOOGLE_CALENDAR_STARTED", "task_id": task_id, "user_id": user_id},
    )

    # ledger note: google calendar operation starting
    try:
        ledger.append(
            "google_calendar_started",
            {"task_id": task_id, "user_id": user_id},
            trace_id=task_id,
        )
    except Exception:
        logger.exception("Failed to append google_calendar_started to audit ledger")

    try:
        # Persist record using task-scoped async session from pooled engine
        async with _worker_sessionmaker() as session:

            # Ensure gcal_service uses the task-scoped DB session
            gcal_service = await get_gcal_service(user_id=user_id, session=session)

            gcal_response = await gcal_service.create_event_with_meet(func_call)
            meeting_link = gcal_response.get("meeting_link")

            record = CalendarEventDB(
                user_id=user_id,
                task_id=task_id,
                request_text=request_text,
                summary=func_call.summary,
                start_time=func_call.start.date_time,
                end_time=func_call.end.date_time,
                attendees=[str(email) for email in func_call.attendees],
                meeting_link=meeting_link,
                google_event_id=gcal_response.get("event_id"),
                status=gcal_response.get("status", "scheduled"),
                raw_function_call=func_call.model_dump(),
            )
            session.add(record)
            await session.commit()

            # Append successful creation to ledger
            ledger.append(
                "google_calendar_created",
                {
                    "task_id": task_id,
                    "user_id": user_id,
                    "google_event": {
                        k: v for k, v in gcal_response.items() if k != "raw"
                    },
                    "summary": func_call.summary,
                },
                trace_id=task_id,
            )

        result = {
            "function_call": func_call.model_dump(),
            "google_calendar_event": gcal_response,
            "meeting_link": meeting_link,
        }

        publish_task_event(
            task_id,
            {
                "type": "TASK_COMPLETED",
                "task_id": task_id,
                "user_id": user_id,
                "result": result,
            },
        )

        return result

    finally:
        # 1. Crucial: Dispose of the engine before asyncio.run() closes the loop
        await _worker_engine.dispose()
        # 2. Cleanly close globally defined Redis connection & reset singleton for next task
        await close_redis()
        # shared ledger remains running; do not stop here


@celery_app.task(
    bind=True,
    name="tasks.execute_calendar_schedule_task",
    soft_time_limit=1200,
    time_limit=1500,
    max_retries=5,
    acks_late=True,
)
def execute_calendar_schedule_task(
    self,
    user_id: int,
    request_text: str,
):
    """
    This is deliberately not:
    autoretry_for=(Exception,)

    Automatically retrying every exception is a bad production strategy because malformed model output,
    invalid OAuth credentials, invalid attendee addresses, programming errors, and other permanent
    failures don't become transient merely because Celery is feeling optimistic.
    Now failed jobs don't simply disappear into the Celery abyss.
    """
    task_id = self.request.id

    try:
        return asyncio.run(
            _execute_schedule(
                task_id=task_id,
                user_id=user_id,
                request_text=request_text,
            )
        )
    except SQLAlchemyError as exc:
        # Gracefully handle DB errors without failing the celery task execution
        logger.error(
            "Database error occurred during schedule task: %s", exc, exc_info=True
        )
        publish_task_event(
            task_id,
            {
                "type": "TASK_FAILED_DB_ERROR",
                "task_id": task_id,
                "error": str(exc),
            },
        )
        return {
            "status": "error",
            "message": "Database error occurred",
            "details": str(exc),
        }

    except HttpError as exc:
        status = getattr(exc.resp, "status", None)
        if status not in TRANSIENT_HTTP_CODES:
            raise

        try:
            raise self.retry(
                exc=exc,
                countdown=(2**self.request.retries),
            )
        except MaxRetriesExceededError:
            publish_task_event(
                task_id,
                {
                    "type": "TASK_DEAD_LETTER",
                    "task_id": task_id,
                    "error": str(exc),
                },
            )
            raise

    except TimeoutError as exc:
        try:
            raise self.retry(
                exc=exc,
                countdown=(2**self.request.retries),
            )
        except MaxRetriesExceededError:
            push_dead_letter(
                task_id=task_id,
                user_id=user_id,
                request_text=request_text,
                error=str(exc),
            )
            raise
