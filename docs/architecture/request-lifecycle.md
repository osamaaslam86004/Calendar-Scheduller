# Request Lifecycle & Asynchronous Message Bus

All scheduling operations execute asynchronously through a multi-stage request pipeline that ensures idempotency, data integrity, and strict request tracing.

---

## End-to-End Sequence Diagram

```mermaid
sequenceDiagram
    autonumber
    actor Client
    participant API as FastAPI Gateway
    participant Bus as Async Message Bus
    participant Redis as Redis Cache/Broker
    participant Worker as Celery Worker
    participant LLM as LLM Engine
    participant GCal as Google Calendar API

    Client->>API: POST /api/v1/async-schedule
    Note over Client,API: Headers: X-Tenant-Context, X-Idempotency-Key
    
    API->>API: Validate Tenant Context & Correlation ID
    API->>Bus: check_idempotency(key, payload)
    
    alt Idempotency Key Exists & Payload Matches
        Bus-->>API: Return Cached Pending/Completed Result
        API-->>Client: 200 OK / 202 Accepted (Cached Task)
    else Idempotency Key Exists & Fingerprint Mismatch
        Bus-->>API: Raise PayloadMismatchError
        API-->>Client: 422 Unprocessable Entity
    else New Request
        Bus->>Redis: Set Key & SHA-256 Fingerprint (TTL: 3600s)
        API->>Redis: Enqueue Task to Celery Queue
        API-->>Client: 202 Accepted (task_id)
    end

    Note over Worker: Asynchronous Worker Execution
    Redis->>Worker: Consume Task Payload
    Worker->>Worker: Publish WebSocket Event (TASK_STARTED)
    Worker->>LLM: Parse Prompt to Function Schema
    LLM-->>Worker: Structured Calendar Arguments
    Worker->>GCal: Create Event with Google Meet Link
    GCal-->>Worker: Event ID & Hangout URL
    Worker->>Redis: Store Result & Update State
    Worker->>Worker: Publish WebSocket Event (TASK_COMPLETED)
```

---

## SHA-256 Payload Fingerprinting & Idempotency

The `AsyncMessageBus` (`messaging/bus.py`) guarantees that duplicate network requests or retries cannot duplicate calendar appointments:

1. **Fingerprint Computation**: The raw JSON request payload is canonicalized (`sort_keys=True`) and hashed using SHA-256.
2. **Key Binding**: The calculated fingerprint is bound to the client's `X-Idempotency-Key` in Redis under `idempotency:{key}` with a default TTL of 3600 seconds (1 hour).
3. **Payload Mismatch Rejection**: Reusing an `X-Idempotency-Key` with modified prompt text or parameters triggers a `PayloadMismatchError` and yields an **HTTP 422** response, preventing replay attacks and payload tampering.

---

## Jittered Exponential Backoff

Transient execution failures within the message bus or Celery tasks apply full-jittered exponential backoff:

$$\text{Delay} = \text{random}(0, \, \min(\text{max\_backoff}, \, \text{base} \times 2^{\text{attempt}}))$$

This prevents thundering-herd scenarios during upstream Google API rate-limiting events or transient network blips.