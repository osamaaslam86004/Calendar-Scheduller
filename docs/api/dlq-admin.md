# API Reference: Admin DLQ Management

Administrative routes for dead-letter queue (DLQ) inspection, dry-run simulation, and safe message replay (`routes/dlq_admin.py`).

!!! warning "Master Key Authentication Required"
    All DLQ administrative endpoints require master key authentication via `X-Admin-Key` or `Authorization: Bearer <SECURITY__APP_MASTER_KEY>`.

---

## 1. List Dead-Letter Queue Messages

```http
GET /admin/dlq/
```

### Response (`200 OK`)

```json
[
  {
    "task_id": "3d1c7c7b-6d7e-4f5a-bf1d-f7af6acdc198",
    "failed_at": "2026-11-04T02:45:10Z",
    "error_message": "Google Calendar API HTTP 503 Service Unavailable",
    "retry_count": 3,
    "payload": {
      "request_text": "Schedule client sync tomorrow 4pm",
      "tenant_id": "tenant_enterprise_99"
    }
  }
]
```

---

## 2. Dry-Run DLQ Replay Simulation

```http
POST /admin/dlq/dry-run
```

Simulates replay execution without modifying database or external calendar state, returning an audit HMAC signature.

### Request Body

```json
{
  "task_id": "3d1c7c7b-6d7e-4f5a-bf1d-f7af6acdc198"
}
```

### Response (`200 OK`)

```json
{
  "task_id": "3d1c7c7b-6d7e-4f5a-bf1d-f7af6acdc198",
  "status": "DRY_RUN_SUCCESS",
  "parsed_payload": {
    "summary": "Client sync",
    "start_time": "2026-11-05T16:00:00Z"
  },
  "hmac_signature": "a6f4e29b10..."
}
```

---

## 3. Idempotent DLQ Replay

```http
POST /admin/dlq/replay
```

Requeues a DLQ message safely using Redis locks and idempotency tracking.

### Headers

| Header | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `X-Idempotency-Key` | `String (UUID)` | **Yes** | Prevents duplicate manual replay executions. |

### Request Body

```json
{
  "task_id": "3d1c7c7b-6d7e-4f5a-bf1d-f7af6acdc198"
}
```

### Response (`200 OK`)

```json
{
  "message": "DLQ task requeued successfully",
  "new_task_id": "9a8b7c6d-5e4f-3a2b-1c0d-9e8f7a6b5c4d",
  "status": "REQUEUED"
}
```
