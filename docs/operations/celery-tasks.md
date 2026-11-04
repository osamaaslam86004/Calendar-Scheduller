# Background Workers & Celery Tasks

Background processing is powered by Celery worker processes using Redis as broker and result storage.

---

## Worker Task Inventory

| Task Name | Module File | Queue | Description |
| :--- | :--- | :--- | :--- |
| `tasks.execute_calendar_schedule` | `tasks/google_calender_meeting.py` | `calendar` | Parses intent via LLM engine and creates Google Calendar event. |
| `tasks.rotate_oauth_tokens` | `tasks/refresh_oauth_tokens.py` | `auth` | Periodic task performing atomic token refreshes across active users. |
| `tasks.publish_task_event` | `tasks/events.py` | `events` | Publishes structured execution lifecycle events to Redis channels. |
| `tasks.escalate_to_dlq` | `tasks/dlq.py` | `dlq` | Handles permanent task failures and records dead-letter database state. |

---

## Running Workers in Production

### Start General Worker

```bash
celery -A celery_app.celery_app worker --loglevel=info -Q calendar,auth,events -c 4
```

### Start Periodic Beat Scheduler

```bash
celery -A celery_app.celery_app beat --loglevel=info
```
