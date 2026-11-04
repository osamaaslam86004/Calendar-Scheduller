# Calendar Scheduling API

A production-oriented FastAPI backend service for converting natural-language meeting requests into structured Google Calendar events. The application authenticates users with Google OAuth, parses scheduling intent with an LLM engine, schedules background work via Celery and Redis, enforces multi-tenant security and RBAC, tracks audit events in an immutable ledger, and streams real-time task progress over WebSockets.

This service translates unstructured prompt inputs into validated function-call schemas:
* "Schedule a 30-minute design review for tomorrow at 3 PM with jane@example.com"
* "Book a customer call next Tuesday at 9:30 AM with bob@company.com and alice@company.com"
* "Create a weekly sync every Friday at 11:00 AM"

The system handles intent extraction, quota and rate enforcement, OAuth token lifecycle management, idempotent execution, and event creation with Google Meet integration, while maintaining real-time telemetry and operational observability.

---

## What this project is

This service is a production-grade scheduling infrastructure with:

* **Google OAuth Authentication & Managed Token Lifecycle**: OAuth2 flow with Redis-locked concurrent token refresh, 45-second grace window, and token-family reuse detection.
* **LLM Intent Extraction Engine**: Dual-provider fallback support (Gemini and OpenRouter) with schema validation via Pydantic models.
* **Multi-Tenant Context & RBAC**: Signed tenant JWT claim enforcement (`X-Tenant-Context`), request isolation, and correlation ID (`X-Correlation-ID`) propagation.
* **Async Message Bus & Idempotent Processing**: SHA-256 payload fingerprinting bound to idempotency keys (`X-Idempotency-Key`), payload mismatch detection (returning HTTP 422), and exponential backoff retries.
* **Dynamic Feature Flags & Anomaly Detection Engine**: Multi-lens system health scoring (P95 latency, error rate, CPU/memory utilization, queue depth), composite health scoring (0–100), circuit breaker auto-tripping, and dynamic rate-limiter adaptation.
* **Tamper-Evident Audit Ledger**: Append-only immutable log with HMAC-SHA256 block chaining, automated PII masking (emails, SSNs, credit cards, phone numbers), and chain integrity verification.
* **Advanced DLQ & Replay Administration**: Admin management endpoints (`/admin/dlq/`), dry-run validation with HMAC audit signatures, and automatic 3-retry quarantine.
* **Real-Time Task Streaming**: WebSocket event stream (`/ws/events/{task_id}`) emitting fine-grained lifecycle notifications.
* **Operational Monitoring Dashboard**: Live endpoints and an embedded interactive Chart.js frontend (`/api/v1/monitoring/dashboard-html`) for percentile latency tracking, anomaly alerts, and dynamic policy inspection.

---

## Project architecture

The codebase follows a modular, layered architecture separating core infrastructure, security middleware, API routers, background tasks, telemetry, and business domain logic.

```text
Calendar Scheduling API
├── main.py                           # FastAPI bootstrap, middleware stack, and lifespan hooks
├── celery_app.py                     # Celery background application setup and worker discovery
├── engine.py                         # LLM provider engine (Gemini & OpenRouter fallbacks)
├── auth.py                           # Core OAuth logic and security dependencies
├── schemas.py                        # Pydantic request/response models & contract schemas
├── dlq.py                            # Redis DLQ queue connection and management helpers
├── docker-compose.yaml               # Infrastructure stack (PostgreSQL + Redis)
├── Dockerfile                        # Production multi-stage Docker build file
├── requirements.txt                  # Python dependencies
├── alembic.ini                       # Database migration configuration
├── flags/                            # Feature flagging & anomaly detection core
│   └── engine.py                     # IntegratedFlagAnomalyManager & AnomalyEngine
├── messaging/                        # Asynchronous messaging infrastructure
│   └── bus.py                        # AsyncMessageBus with SHA-256 fingerprinting & backoff
├── middleware/                       # HTTP pipeline middleware stack
│   ├── anomaly_detection.py          # Latency recording and anomaly trigger middleware
│   ├── request_correlation.py        # X-Correlation-ID request tracking middleware
│   ├── tenant_context.py             # Tenant context verification and rate limiting
│   ├── tenant_guard.py               # Tenant context assertion guard
│   └── tenant_rbac.py                # Tenant role-based access control middleware
├── migrations/                       # Alembic schema migration scripts
│   ├── env.py
│   └── versions/
├── models/                           # SQLAlchemy / SQLModel database entities
│   ├── auth_db.py                    # OAuth token persistence and family lineage
│   ├── base.py                       # Declarative Base metadata
│   ├── dlq_db.py                     # Dead-letter queue persistence models
│   ├── google_calender_db.py         # Google Calendar event records
│   ├── llm_response_tracker_db.py    # LLM request/token cost usage tracking
│   └── user_db.py                    # User profile entities
├── routes/                           # FastAPI API routers
│   ├── admin_analytics.py            # LLM token usage and cost analytics endpoints
│   ├── auth.py                       # Google OAuth authentication endpoints
│   ├── dlq_admin.py                  # DLQ inspection, dry-run, and replay router
│   ├── event_parser.py               # Direct LLM function-call extraction endpoint
│   ├── feature_flags_admin.py        # Dynamic flag and limit control router
│   ├── meeting_scheduler.py          # Asynchronous meeting scheduling router
│   ├── monitoring_dashboard.py       # Health snapshot, percentiles, and UI router
│   ├── task_status.py                # Async task polling router
│   ├── tenant_resources.py           # Tenant scope isolation and action router
│   └── websocket_events.py           # Real-time WebSocket task status streamer
├── services/                         # Core business domain logic
│   ├── google_calender/              # Google Calendar API integration service
│   └── usage_tracker/                # LLM cost and usage tracking service
├── tasks/                            # Celery background tasks
│   ├── dlq.py                        # DLQ escalation and dead-letter tasks
│   ├── events.py                     # Task status event publication
│   ├── google_calender_meeting.py    # Async meeting creation background worker
│   └── refresh_oauth_tokens.py       # Atomic OAuth token refresh and rotation worker
├── telemetry/                        # Observability and audit logging
│   ├── audit_ledger.py               # Immutable HMAC-SHA256 chained audit ledger
│   └── manager.py                    # Global ledger lifecycle and thread context helpers
├── utilities/                        # Security, caching, and rate limiting helpers
│   ├── adaptive_throttling.py        # Dynamic throttle multiplier calculations
│   ├── anomaly_detector.py           # Latency percentile tracker and anomaly detection
│   ├── cache.py                      # Redis cache accessors
│   ├── circuit_breaker.py            # Service circuit breaker pattern implementation
│   ├── feature_flags.py              # Redis Pub/Sub feature flag subscriber
│   ├── redis_scripts.py              # Lua scripts for atomic token-bucket operations
│   ├── security.py                   # JWT verification, tenant context, PII masking
│   └── tokenizer.py                  # Prompt token counter
└── tests/                            # Automated test suite
    ├── unit/                         # Fast isolated unit tests (timing budget enforced)
    ├── integration/                  # Cross-component integration tests
    ├── performance/                  # Concurrency stress and load benchmark suite
    └── e2e/                          # End-to-end user workflow integration checks
```

---

### Request lifecycle

```text
 Client (Web/Mobile)              FastAPI Pipeline               Redis / MessageBus             Celery Worker              Google / LLM API
       │                                 │                                │                           │                           │
       │─── 1. POST /async-schedule ────►│                                │                           │                           │
       │    (X-Tenant-Context,           │                                │                           │                           │
       │     X-Idempotency-Key)          │                                │                           │                           │
       │                                 │─── 2. Correlation & Tenant ───►│                           │                           │
       │                                 │    Guard Validation            │                           │                           │
       │                                 │                                │                           │                           │
       │                                 │─── 3. Check Idempotency ──────►│                           │                           │
       │                                 │    & Payload Fingerprint       │                           │                           │
       │                                 │    (SHA-256)                   │                           │                           │
       │                                 │◄─── 4. Ok / Pending ───────────│                           │                           │
       │                                 │                                │                           │                           │
       │                                 │─── 5. Enqueue Task ───────────────────────────────────────►│                           │
       │◄── 6. 202 Accepted ─────────────│                                │                           │                           │
       │    (task_id: "uuid")            │                                │                           │                           │
       │                                 │                                │                           │─── 7. Extract Intent ────►│
       │                                 │                                │                           │    (Gemini / OpenRouter)  │
       │                                 │                                │                           │◄── 8. Function Schema ────│
       │                                 │                                │                           │                           │
       │                                 │                                │                           │─── 9. Refresh Token ─────►│
       │                                 │                                │                           │    (OAuth Grace Window)   │
       │                                 │                                │                           │                           │
       │                                 │                                │                           │─── 10. Create Event ─────►│
       │                                 │                                │                           │     (Calendar + Meet)     │
       │                                 │                                │                           │◄── 11. Event ID ──────────│
       │                                 │                                │                           │                           │
       │◄── 12. WS Progress Stream ───────────────────────────────────────────────────────────────────│                           │
       │    (TASK_COMPLETED)             │                                │                           │                           │
```

---

### Core components

* **FastAPI Web Framework**: Provides asynchronous HTTP request processing, dependency injection, and automatic OpenAPI schema generation.
* **Celery & Redis**: Offloads LLM evaluation and Google Calendar API synchronization to background workers with Redis as broker and result backend.
* **Multi-Tenant Security Subsystem**: Evaluates cryptographically signed JWTs, isolates resource access per tenant, and enforces tenant authorization.
* **Async Message Bus**: Guarantees request processing idempotency, calculates SHA-256 payload fingerprints, and prevents duplicate task execution.
* **Dynamic Feature Flags & Anomaly Engine**: Evaluates system metrics in real time, computes system health scores (0–100), and dynamically scales rate limits or trips circuit breakers.
* **Tamper-Evident Audit Ledger**: Records audit events to an immutable append-only JSON file using HMAC-SHA256 block hashing and regex PII masking.
* **OAuth Token Manager**: Handles OAuth refresh token rotation using Redis distributed locks, serving fresh access tokens within a 45-second grace window and quarantining compromised token families.
* **Monitoring Dashboard**: Serves metric snapshots, latency percentiles (P50/P95/P99), and an interactive HTML/Chart.js operational panel.

---

## Features

* **Natural Language Meeting Scheduling**: Extracts start time, duration, attendees, summary, and recurrence rules from raw text inputs.
* **Google OAuth2 Authentication**: End-to-end OAuth flow with atomic token refresh and family compromise protection.
* **Async Task Execution**: Returns task identifiers instantly while processing Calendar integrations in Celery workers.
* **Idempotency & Fingerprint Guard**: Prevents duplicate executions by hashing JSON request bodies and storing state in Redis.
* **Real-Time Progress Streaming**: Emits WebSocket events (`TASK_STARTED`, `LLM_STARTED`, `LLM_COMPLETED`, `GOOGLE_CALENDAR_STARTED`, `TASK_COMPLETED`, `TASK_DEAD_LETTER`).
* **Adaptive System Protection**: System health monitoring dynamically adjusts rate-limiting thresholds and trips circuit breakers when P95 latency or error rates spike.
* **Immutable Audit Trail**: SHA-256 HMAC chained ledger ensures forensic auditability with automatic PII sanitization.
* **Dead-Letter Queue Management**: Dedicated DLQ router supporting safe inspection, dry-run simulation with HMAC signatures, manual requeueing, and automatic 3-retry quarantine.
* **LLM Usage Analytics**: Tracks prompt/completion token counts, estimated API costs, and provider performance metrics.
* **Live Monitoring Frontend**: Real-time percentile chart rendering and policy state adjustment UI.

---

## Project setup

### Prerequisites

* **Python**: 3.11 or higher
* **Containerization**: Docker & Docker Compose
* **Databases**: PostgreSQL 15+ and Redis 7+
* **External API Keys**: Google OAuth Client Credentials, Google Gemini API Key, and/or OpenRouter API Key

---

### 1. Clone repository and create virtual environment

```bash
git clone <repository-url>
cd calendar-scheduling-api

# Create Python virtual environment
python -m venv env

# Activate environment
# On Linux/macOS:
source env/bin/activate
# On Windows PowerShell:
.\env\Scripts\Activate.ps1

# Upgrade pip and install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

---

### 2. Configure environment variables

Create a `.env` file in the root directory:

```dotenv
# Application Settings
APP__NAME="Calendar Scheduling API"
APP__ENV="development"
APP__DEBUG="false"
APP__LOG_LEVEL="INFO"
APP__API_V1_PREFIX="/api/v1"
APP__DESCRIPTION="Natural Language Calendar Scheduling API Service"

# Security & Master Keys
SECURITY__SESSION_SECRET="replace-with-a-64-character-random-secret-key"
SECURITY__APP_MASTER_KEY="replace-with-a-64-character-hex-master-key"
SECURITY__ALLOW_ORIGINS="http://localhost:3000"
SECURITY__ALLOW_CREDENTIALS="true"
SECURITY__ALLOW_METHODS="*"
SECURITY__ALLOW_HEADERS="*"

# Database Connections
DB__ASYNC_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/calendar_service"
DB__SYNC_URL="postgresql+psycopg2://postgres:postgres@localhost:5432/calendar_service"
DB__POOL_SIZE=20
DB__MAX_OVERFLOW=10

# Redis Cache & Broker
REDIS__URL="redis://localhost:6379/0"
REDIS__SYNC_DB=0
REDIS__ASYNC_DB=1

# Google OAuth Credentials
GOOGLE_OAUTH__CLIENT_ID="your-client-id.apps.googleusercontent.com"
GOOGLE_OAUTH__CLIENT_SECRET="your-client-secret"
GOOGLE_OAUTH__REDIRECT_URL="http://localhost:8000/auth/callback"

# LLM Provider API Keys
LLM__GEMINI_API_KEY="your-gemini-api-key"
LLM__OPEN_ROUTER_API_KEY="your-openrouter-api-key"
```

---

### 3. Start local infrastructure

Launch PostgreSQL and Redis using Docker Compose:

```bash
docker compose up -d postgres redis
```

Apply database migrations using Alembic:

```bash
alembic upgrade head
```

---

### 4. Start the FastAPI server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

---

### 5. Start the Celery worker process

```bash
celery -A celery_app.celery_app worker --loglevel=info -Q calendar
```

---

### 6. Run the test suite

The codebase includes comprehensive unit, integration, performance, and end-to-end test suites:

```bash
# Run unit tests (offline timing budget enforced)
pytest -q tests/unit -m unit

# Run integration tests
pytest -q tests/integration

# Run performance and stress benchmark suite
pytest -q tests/performance

# Run end-to-end workflow suite
pytest -q tests/e2e

# Run the complete test suite with coverage
pytest --cov=. --cov-report=term-missing
```

---

## How to use this project (API Reference)

### Authentication Router (`/auth`)

#### 1. Initiate Google OAuth Login
* **Method & Path**: `GET /auth/login`
* **Description**: Redirects the user to Google's OAuth2 consent screen.

#### 2. OAuth Callback
* **Method & Path**: `GET /auth/callback?code={code}&state={state}`
* **Description**: Exchanges the authorization code for access and refresh tokens, creates user session, and stores token lineage.

#### 3. Logout
* **Method & Path**: `POST /auth/logout`
* **Description**: Clears session cookies and invalidates session context.

---

### Scheduling Router (`/api/v1`)

#### 1. Asynchronous Meeting Scheduling
* **Method & Path**: `POST /api/v1/async-schedule`
* **Headers**:
  * `X-Tenant-Context`: Signed JWT string (Required)
  * `X-Idempotency-Key`: Unique UUID string (Required)
  * `X-Correlation-ID`: Request tracking UUID (Optional)
* **Request Body**:
  ```json
  {
    "request_text": "Schedule a 30-minute design review for tomorrow at 3 PM with jane@example.com"
  }
  ```
* **Response (202 Accepted)**:
  ```json
  {
    "task_id": "3d1c7c7b-6d7e-4f5a-bf1d-f7af6acdc198",
    "status": "PENDING"
  }
  ```
* **Error Response (422 Unprocessable Entity)**: Returned if the `X-Idempotency-Key` is reused with a modified request payload.

#### 2. Synchronous Event Parsing
* **Method & Path**: `POST /api/v1/parse-event`
* **Headers**:
  * `X-Tenant-Context`: Signed JWT string
* **Request Body**:
  ```json
  {
    "request_text": "Book a sync call with team@company.com next Monday at 10 AM"
  }
  ```
* **Response (200 OK)**:
  ```json
  {
    "function_name": "create_calendar_event",
    "arguments": {
      "summary": "Sync call",
      "start_time": "2026-11-09T10:00:00Z",
      "end_time": "2026-11-09T10:30:00Z",
      "attendees": ["team@company.com"],
      "recurrence": null
    }
  }
  ```

#### 3. Task Status Polling
* **Method & Path**: `GET /api/v1/tasks/{task_id}`
* **Response (200 OK)**:
  ```json
  {
    "task_id": "3d1c7c7b-6d7e-4f5a-bf1d-f7af6acdc198",
    "status": "SUCCESS",
    "result": {
      "event_id": "gcal_event_998234",
      "html_link": "https://calendar.google.com/calendar/event?eid=...",
      "hangout_link": "https://meet.google.com/abc-defg-hij"
    }
  }
  ```

---

### Real-Time WebSocket Router (`/ws`)

#### Progress Event Stream
* **WebSocket Endpoint**: `ws://localhost:8000/ws/events/{task_id}`
* **Description**: Streams structured JSON status updates as the Celery task progresses.
* **Emitted Event Types**:
  * `TASK_STARTED`
  * `LLM_STARTED`
  * `LLM_COMPLETED`
  * `GOOGLE_CALENDAR_STARTED`
  * `TASK_COMPLETED`
  * `TASK_FAILED_DB_ERROR`
  * `TASK_DEAD_LETTER`

---

### Multi-Tenant Resource Router (`/api/v1/tenant-resources`)

#### 1. Get Tenant Information
* **Method & Path**: `GET /api/v1/tenant-resources/`
* **Headers**: `X-Tenant-Context` (Signed JWT)
* **Response (200 OK)**: Returns tenant identity, organization boundaries, and permitted operations.

#### 2. Execute Tenant Action
* **Method & Path**: `POST /api/v1/tenant-resources/action`
* **Headers**: `X-Tenant-Context` (Signed JWT)
* **Request Body**: Tenant-scoped resource payload.

---

### Admin & Analytics Router (`/api/v1/admin`)

#### LLM Token Usage Analytics
* **Method & Path**: `GET /api/v1/admin/llm-analytics`
* **Headers**: `X-Admin-Key` or `Authorization: Bearer <master-key>`
* **Response (200 OK)**: Aggregated token counts, prompt/completion cost breakdown, cache hit rates, and provider latency statistics.

---

### Dead-Letter Queue (DLQ) Admin Router (`/admin/dlq`)

All DLQ admin routes require master key authentication via `X-Admin-Key` or `Authorization: Bearer <key>`.

#### 1. List DLQ Messages
* **Method & Path**: `GET /admin/dlq/`
* **Response (200 OK)**: List of dead-lettered task records.

#### 2. DLQ Message Dry-Run Validation
* **Method & Path**: `POST /admin/dlq/dry-run`
* **Request Body**: `{"task_id": "uuid"}`
* **Response (200 OK)**:
  ```json
  {
    "task_id": "uuid",
    "status": "DRY_RUN_SUCCESS",
    "parsed_payload": { ... },
    "hmac_signature": "a6f4e..."
  }
  ```

#### 3. Idempotent DLQ Replay
* **Method & Path**: `POST /admin/dlq/replay`
* **Headers**: `X-Idempotency-Key` (Required)
* **Request Body**: `{"task_id": "uuid"}`
* **Response (200 OK)**: Requeue status and new task metadata. Messages exceeding 3 replay attempts are automatically quarantined.

#### 4. Requeue Single Entry
* **Method & Path**: `POST /admin/dlq/requeue?task_id={task_id}`

#### 5. Requeue All Entries
* **Method & Path**: `POST /admin/dlq/requeue_all`

#### 6. Purge DLQ Entry
* **Method & Path**: `DELETE /admin/dlq/{task_id}`

---

### Dynamic Feature Flags Admin Router (`/api/v1/admin/flags`)

#### 1. Update Feature Flag
* **Method & Path**: `POST /api/v1/admin/flags/set`
* **Request Body**: `{"flag_name": "enable_openrouter_fallback", "value": true}`

#### 2. Get Feature Flag State
* **Method & Path**: `GET /api/v1/admin/flags/get/{flag_name}`

#### 3. Set Rate Limit Capacity
* **Method & Path**: `POST /api/v1/admin/flags/rate-limit/set`
* **Request Body**: `{"capacity": 200}`

#### 4. Set Circuit Breaker Threshold
* **Method & Path**: `POST /api/v1/admin/flags/circuit-breaker/set`
* **Request Body**: `{"failure_threshold": 5.0}`

#### 5. Set Token Refill Rate
* **Method & Path**: `POST /api/v1/admin/flags/token-refill/set`

#### 6. Throttle Background Jobs
* **Method & Path**: `POST /api/v1/admin/flags/background-job/throttle`

#### 7. Toggle System Feature
* **Method & Path**: `POST /api/v1/admin/flags/feature/toggle?feature_name={name}&enabled={true|false}`

---

### Monitoring Router (`/api/v1/monitoring`)

#### 1. System Health Snapshot
* **Method & Path**: `GET /api/v1/monitoring/health-snapshot`
* **Response (200 OK)**: System health score, percentile metrics (P50/P95/P99), total anomalies, and circuit breaker status.

#### 2. Operation Percentiles
* **Method & Path**: `GET /api/v1/monitoring/percentiles/{operation}`

#### 3. Active Anomalies
* **Method & Path**: `GET /api/v1/monitoring/anomalies`

#### 4. Active Policy State
* **Method & Path**: `GET /api/v1/monitoring/policy-state`

#### 5. Metrics History
* **Method & Path**: `GET /api/v1/monitoring/metrics-history/{operation}`

#### 6. Live Dashboard HTML
* **Method & Path**: `GET /api/v1/monitoring/dashboard-html`
* **Response**: Embedded interactive Chart.js frontend visualizing system metrics in real time.

---

## Runtime design notes

### 1. Dynamic Feature Flags & Anomaly Detection Engine
Implemented across `flags/engine.py`, `utilities/feature_flags.py`, `utilities/anomaly_detector.py`, and `utilities/adaptive_throttling.py`.

* **Multi-Lens System Health Scoring**: Continuously monitors four core operational signals:
  1. P95 Latency (warning threshold: 5000 ms)
  2. Error Rate % (warning threshold: 10.0%)
  3. CPU and Memory Resource Utilization %
  4. Celery Queue Depth
* **Composite Health Score (0–100)**: Calculates a system-wide composite score. If the health score drops below **70.0**, the engine automatically trips dependent service circuit breakers into a half-open state.
* **Dynamic Rate-Limiter Adaptation**: Adjusts nominal request capacity based on system health:
  $$R_{adapted} = R_{nominal} \times \frac{\text{health}}{100}$$
* **Redis Pub/Sub Real-Time Invalidation**: Feature flag and configuration changes broadcast over Redis channels (`FeatureFlagSubscriber`), applying updates instantly across API nodes without requiring server restarts.

---

### 2. Async Message Bus & Idempotence Control
Implemented in `messaging/bus.py`.

* **SHA-256 Payload Fingerprinting**: Computes a deterministic SHA-256 hash of incoming JSON request payloads bound to client-provided `X-Idempotency-Key` headers.
* **Payload Mismatch Detection**: If an existing `X-Idempotency-Key` is received with a modified payload fingerprint, the bus raises a `PayloadMismatchError` and immediately returns **HTTP 422 Unprocessable Entity**, preventing request tampering and duplicate submission attacks.
* **Full-Jitter Exponential Backoff**: Uses standard randomized full-jitter backoff delays to prevent retry thundering herds during transient infrastructure failures.

---

### 3. Security, Tenant Context & RBAC Middleware
Implemented across `middleware/tenant_context.py`, `middleware/tenant_guard.py`, `middleware/tenant_rbac.py`, `middleware/anomaly_detection.py`, `middleware/request_correlation.py`, and `utilities/security.py`.

* **Signed JWT Tenant Enforcement**: Validates cryptographically signed JWT claims in the `X-Tenant-Context` header. Rejects unauthorized access with `HTTP 401 Unauthorized`.
* **Tenant Isolation & ABAC/RBAC**: Verifies that the requesting tenant has explicit permission for the requested resource and operation (`bounds.allowed_operations`), preventing cross-tenant data leaks.
* **Correlation ID Propagation**: Enforces or generates a unique `X-Correlation-ID` for every HTTP request, attaching it to request state, background Celery tasks, and audit logs for end-to-end distributed tracing.
* **Anomaly Detection Middleware**: Intercepts HTTP request pipelines, records operation latency (`METHOD /path`), and triggers adaptive throttling when latency anomalies are detected.

---

### 4. Tamper-Evident Immutable Audit Ledger
Implemented in `telemetry/audit_ledger.py` and `telemetry/manager.py`.

* **Cryptographic Block Chaining**: Implements an append-only ledger stored as newline-delimited JSON (`audit_ledger.json`). Each block contains an HMAC-SHA256 signature calculated over its fields (index, timestamp, correlation_id, event_type, tenant_id, payload) chained directly to the previous block's hash.
* **Automated PII Masking**: Intercepts all logged payloads with regular expressions, automatically redacting sensitive user information:
  * Emails: `user@domain.com` $\rightarrow$ `[MASKED]@domain.com`
  * Social Security Numbers: `xxx-xx-xxxx` $\rightarrow$ `[MASKED_SSN]`
  * Credit Card Numbers: 13–16 digits $\rightarrow$ `[MASKED_CC]`
  * Phone Numbers: Masked leaving only the last 2 digits.
* **Chain Integrity Verification**: Provides automated verification routines (`verify_chain_integrity()`) that walk the ledger file to detect block deletion, insertion, or signature tampering.

---

### 5. Advanced DLQ & Replay Administration
Implemented in `routes/dlq_admin.py`, `tasks/dlq.py`, and `models/dlq_db.py`.

* **Tenant-Scoped Replay Security**: Ensures DLQ replay requests (`/admin/dlq/replay`) evaluate tenant claims (`dlq:replay`) alongside admin master keys.
* **Idempotent Requeueing**: Enforces `X-Idempotency-Key` headers backed by Redis distributed locks to prevent duplicate execution during manual administrative interventions.
* **Dry-Run Inspection**: Evaluates failed payloads without modifying database state, generating an HMAC audit signature to verify dry-run integrity.
* **Automated Replay Quarantine**: Tracks replay attempts per message; messages that fail **3 consecutive replay attempts** are automatically moved to a permanent quarantine state.

---

### 6. OAuth Token Family Rotation & Compromise Protection
Implemented in `tasks/refresh_oauth_tokens.py` (`OAuthTokenManager`).

* **Concurrent Refresh Protection**: Prevents race conditions during token refresh across distributed Celery workers by acquiring Redis distributed locks (`lock:oauth:refresh:{user_id}`).
* **45-Second Grace Window**: Serves cached fresh access tokens from Redis (`oauth:grace:{refresh_token}`) for 45 seconds to handle near-simultaneous requests during an ongoing refresh.
* **Token Family Tracking & Quarantine**: Maintains refresh token lineage in Redis sets (`oauth:token_family:{user_id}`). If a previously rotated or revoked refresh token is presented, the system flags a security breach, revokes the entire token family into `oauth:revoked_tokens:{user_id}`, and invalidates active DB credentials.

---

### 7. Real-Time Monitoring & Live Dashboard
Implemented in `routes/monitoring_dashboard.py`.

* **Percentile Metrics Calculation**: Tracks rolling window latencies to provide exact P50, P95, and P99 metrics per API route.
* **Embedded Interactive UI**: Serves a single-page interactive HTML interface powered by Chart.js at `/api/v1/monitoring/dashboard-html`, displaying live percentile charts, anomaly alerts, and real-time policy configuration state.

---

## Recommended production hardening

For production deployment, ensure the following controls are configured:

* ✅ **TLS Termination & Secure Cookies**: Configure reverse proxy (Nginx/Traefik) with HTTPS and enforce `Secure` and `SameSite=Lax` session cookies.
* ✅ **Secret Management**: Externalize `SECURITY__APP_MASTER_KEY` and database secrets to a cloud secret store (AWS Secrets Manager, HashiCorp Vault).
* ✅ **Strict CORS Rules**: Restrict `SECURITY__ALLOW_ORIGINS` strictly to authorized domain origins.
* ✅ **Tenant Isolation & RBAC**: Enforce tenant JWT signatures across all operational endpoints.
* ✅ **Audit Ledger Security**: Store the HMAC secret key outside the deployment container and archive ledger blocks to immutable object storage (AWS S3 Object Lock).
* ✅ **Automated CI/CD Verification**: Require successful unit, integration, and performance test runs prior to deployment.

---

## TODO / roadmap

* ✅ Add unit and integration test suites covering routes, tasks, and middleware
* ✅ Add API versioning documentation and OpenAPI endpoint contracts
* ✅ Improve error taxonomy for transient vs permanent failures
* ✅ Add true admin dashboard for task health and DLQ event management
* ✅ Add background health checks for Redis, Postgres, Google APIs, and Celery workers
* ✅ Add structured observability with correlation IDs (`X-Correlation-ID`)
* ✅ Add user-level quotas and tenant-based authorization controls (`X-Tenant-Context`)
* ✅ Add event recurrence support and calendar editing capabilities
* ✅ Add idempotency keys for scheduling requests (`X-Idempotency-Key`)
* ✅ Add WebSockets event stream for task lifecycle updates
* ✅ Add database migration scripts using Alembic
* ✅ Add timezone-aware scheduling prompts and DST-safe wall-clock normalization
* ✅ Add safe DLQ replay, dry-run simulation, quarantine, and audit controls
* ✅ Add dynamic rate-limit adaptation based on system health scoring
* ✅ Add OAuth token family rotation with Redis grace-window and reuse quarantine
* ✅ Add real-time monitoring dashboard with P50/P95/P99 latency percentile visualizations
* ⏳ Deploy to staging environment and verify Pub/Sub flag propagation across multi-node worker clusters
* ⏳ Conduct chaos engineering tests to validate circuit breaker auto-tripping under simulated network latency
* ⏳ Expand automated anomaly detection models to incorporate seasonal decomposition
