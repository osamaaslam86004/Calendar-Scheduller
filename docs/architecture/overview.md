# Systems Architecture & Topology

The **Calendar Scheduling API** is architected around a layered, decoupled service structure separating HTTP request handling, security context validation, asynchronous work distribution, LLM intent parsing, and immutable telemetry recording.

---

## Component Topology

```mermaid
graph TD
    Client[Web/Mobile Client] -->|HTTP / WS| FastAPI[FastAPI App Gateway]
    
    subgraph Middleware Stack
        FastAPI --> Cors[Session & CORS Middleware]
        Cors --> TenantMiddleware[Tenant Context & Guard]
        TenantMiddleware --> CorrelationMiddleware[Correlation ID Tracer]
        CorrelationMiddleware --> AnomalyMiddleware[Anomaly Detection Middleware]
    end
    
    subgraph Core Ingestion & Bus
        AnomalyMiddleware --> MessageBus[Async Message Bus]
        MessageBus -->|SHA-256 Check| RedisCache[(Redis Store)]
    end

    subgraph Background Execution Layer
        FastAPI -->|Enqueue Task| CeleryWorker[Celery Background Workers]
        CeleryWorker -->|Pub/Sub Events| WSHandler[WebSocket Event Stream]
        CeleryWorker -->|Token Refresh| OAuthMgr[OAuth Token Manager]
        CeleryWorker -->|Parse Intent| LLMEngine[LLM Function Engine]
    end

    subgraph Upstream Services
        LLMEngine -->|Primary| Gemini[Google Gemini API]
        LLMEngine -->|Fallback| OpenRouter[OpenRouter API]
        OAuthMgr -->|Refresh| GoogleAuth[Google OAuth Provider]
        CeleryWorker -->|Create Event| GoogleCal[Google Calendar API]
    end

    subgraph Observability & Storage
        CeleryWorker --> Postgres[(PostgreSQL DB)]
        FastAPI --> AuditLedger[Append-Only HMAC Ledger]
        FastAPI --> FlagEngine[Integrated Flag & Anomaly Manager]
    end
```

---

## Architectural Principles

### 1. Strict Layered Isolation
The codebase enforces clear separation of concerns across dedicated modules:
* `routes/`: Public HTTP API definition and dependency injection.
* `middleware/`: HTTP context assertion, rate limiting, and performance tracking.
* `messaging/`: Idempotency enforcement and payload integrity checking.
* `engine.py`: Structured LLM extraction and provider fallback orchestration.
* `tasks/`: Asynchronous job execution, token management, and dead-letter queue escalation.
* `telemetry/`: Tamper-evident ledger writing and distributed audit tracking.

### 2. High-Availability Operational Safeguards
* **Non-blocking API Gateway**: Scheduling requests return `202 Accepted` immediately upon enqueueing.
* **Pub/Sub Dynamic Configuration**: Live adjustments to rate limits, circuit breaker thresholds, and feature flags propagate across worker clusters without service restarts via Redis Pub/Sub (`utilities/feature_flags.py`).
