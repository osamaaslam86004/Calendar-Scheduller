# API Reference: Scheduling & Event Parsing

Primary endpoints for submitting scheduling prompts, parsing function calls, and polling task execution status.

---

## 1. Asynchronous Meeting Scheduling

```http
POST /api/v1/async-schedule
```

Accepts natural-language scheduling requests and enqueues a Celery background task.

### HTTP Headers

| Header | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `X-Tenant-Context` | `String` | **Yes** | Signed tenant JWT claims token. |
| `X-Idempotency-Key` | `String (UUID)` | **Yes** | Client request idempotency key. |
| `X-Correlation-ID` | `String (UUID)` | No | Tracing correlation ID. |

### Request Body

```json
{
  "request_text": "Schedule a 30-minute design review for tomorrow at 3 PM with jane@example.com and bob@example.com"
}
```

### Response (`202 Accepted`)

```json
{
  "task_id": "3d1c7c7b-6d7e-4f5a-bf1d-f7af6acdc198",
  "status": "PENDING"
}
```

### Error Responses

* **`401 Unauthorized`**: Missing or invalid `X-Tenant-Context`.
* **`422 Unprocessable Entity`**: `X-Idempotency-Key` reused with a different request payload fingerprint.

---

## 2. Synchronous Event Parsing

```http
POST /api/v1/parse-event
```

Parses prompt text into structured JSON function schema without enqueueing background Calendar jobs.

### Request Body

```json
{
  "request_text": "Create a weekly team sync every Friday at 11 AM"
}
```

### Response (`200 OK`)

```json
{
  "function_name": "create_calendar_event",
  "arguments": {
    "summary": "Weekly team sync",
    "start_time": "2026-11-06T11:00:00Z",
    "end_time": "2026-11-06T11:30:00Z",
    "attendees": [],
    "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=FR"]
  }
}
```

---

## 3. Poll Task Execution Status

```http
GET /api/v1/tasks/{task_id}
```

### Path Parameters

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `task_id` | `String (UUID)` | **Yes** | Unique task ID returned from `/async-schedule`. |

### Response (`200 OK`)

```json
{
  "task_id": "3d1c7c7b-6d7e-4f5a-bf1d-f7af6acdc198",
  "status": "SUCCESS",
  "result": {
    "event_id": "gcal_event_998234",
    "html_link": "https://calendar.google.com/calendar/event?eid=abc",
    "hangout_link": "https://meet.google.com/abc-defg-hij"
  }
}
```
