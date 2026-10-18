import os
from celery import Celery
from config.settings import settings
from datetime import timedelta

# Use the formatted string URL from central settings
REDIS_URL = settings.redis.url

celery_app = Celery(
    "calendar_tasks",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=[
        "tasks.google_calender_meeting",
        "tasks.refresh_oauth_tokens",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=300,
    task_soft_time_limit=270,
    result_expires=86400,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 3600},
    task_routes={
        "tasks.execute_calendar_schedule_task": {
            "queue": "calendar",
        },
    },
)

# Periodic beat schedule: rotate OAuth refresh tokens every 5 minutes
celery_app.conf.beat_schedule = {
    "rotate-oauth-tokens": {
        "task": "tasks.rotate_oauth_tokens",
        "schedule": timedelta(minutes=5),
    }
}
