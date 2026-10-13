# Calendar Scheduling API

A production-oriented FastAPI service for converting natural-language meeting requests into structured Google Calendar events. The application authenticates users with Google OAuth, parses scheduling intent with an LLM, schedules background work with Celery, and publishes task progress over WebSockets.

This project is designed for use cases where a user says something like:

- "Schedule a 30-minute design review for tomorrow at 3 PM with jane@example.com"
- "Book a customer call next Tuesday at 9:30 AM with bob@company.com and alice@company.com"
- "Create a weekly sync every Friday at 11:00 AM"

The service translates that request into a validated function-call schema, tracks usage and cost, creates the event in Google Calendar with Meet, and returns progress information while the job is running.

## What this project is

This is not just a simple API wrapper around Google Calendar. It is a complete scheduling workflow with:

- Google OAuth-based authentication and token persistence
- LLM-driven extraction of calendar intent from plain text
- Structured output validation using Pydantic schemas
- Background task execution via Celery and Redis
- Rate-limiting and token-budget enforcement
- Real-time task status updates over WebSockets
- Usage tracking and admin analytics for LLM requests
- Dead-letter handling for failed or retryable background jobs
- Redis caching for repeated prompt extraction

At a high level, the system turns user intent into a scheduled calendar event and stores operational metadata so production operators can monitor reliability, cost, and failures.

## Project architecture

The codebase follows a layered architecture with separate concerns for API routes, configuration, persistence, business services, and async background processing.

```text
Calendar Scheduling API
├── main.py                           # FastAPI application bootstrap
├── celery_app.py                     # Celery application setup and autodiscovery
├── engine.py                         # LLM provider abstraction and schema extraction engine
├── auth.py                           # Shared auth utilities and user dependency logic
├── schemas.py                        # Pydantic models / API contracts
├── docker-compose.yaml               # Postgres + Redis local infrastructure
├── requirements.txt                  # Python dependencies
├── alembic.ini                       # Database migration configuration
├── migrations/                       # Alembic migration scripts
├── config/                           # Settings, DB, caching, limiter, logging
│   ├── database.py
│   ├── cache.py
│   ├── limiter.py
│   ├── logging_config.py
│   └── settings.py
├── models/                           # SQLAlchemy / SQLModel database models
│   ├── auth_db.py
│   ├── google_calender_db.py
│   ├── llm_response_tracker_db.py
│   ├── user_db.py
│   └── base.py
├── routes/                           # API endpoints
│   ├── auth.py
│   ├── event_parser.py
│   ├── meeting_scheduler.py
│   ├── task_status.py
│   ├── websocket_events.py
│   ├── admin_analytics.py
│   └── dlq_admin.py
├── services/                         # Business logic services
│   ├── google_calender/
│   └── usage_tracker/
├── tasks/                            # Celery job definitions and task callbacks
│   ├── google_calender_meeting.py
│   ├── events.py
│   ├── dlq.py
│   └── refresh_oauth_tokens.py
├── utilities/                        # Redis scripts, token counting, security helpers
│   ├── cache.py
│   ├── redis_scripts.py
│   ├── security.py
│   ├── tokenizer.py
│   ├── circuit_breaker.py
│   └── templates/
└── env/                              # Local Python environment (if created in workspace)
```

### Request lifecycle

1. User signs in via Google OAuth (`/auth/login` -> `/auth/callback`)
2. A scheduling request is submitted to `POST /api/v1/async-schedule`
3. The service checks rate limits using a Redis-backed dual-bucket limiter
4. A Celery background task is created and assigned a task ID
5. The worker parses the request with the LLM engine and validates it to a schema
6. The system stores usage metadata and publishes progress events
7. Google Calendar API creates the event and optionally attaches Meet
8. Results and task status are returned through polling or WebSocket events

### Core components

- `FastAPI`: public HTTP API and auth flow
- `Celery`: background execution of long-running scheduling tasks
- `Redis`: rate limits, caching, Celery broker/backend, token quotas
- `PostgreSQL`: user records, OAuth tokens, usage logs, calendar event records
- `Google OAuth + Calendar API`: user identity and event creation
- `LLM engine`: structured extraction via Gemini/OpenRouter and schema validation
- `WebSocket event stream`: realtime status updates for UI clients

## What the app does

The service is focused on a single, practical outcome: scheduling meetings from natural language.

### Main capabilities

- Schedule a meeting from a human-written request
- Parse date, time, attendees, summary, and optional recurrence intent
- Create Google Calendar events with Google Meet links
- Persist event metadata and E2E task status
- Track provider usage, model output quality, and token cost
- Surface dead-letter messages for persistent failures
- Support real-time UI updates during task execution
- Enforce quotas to prevent API abuse and unexpected cost spikes

## Features

- Google OAuth login and callback flow
- Structured output generation with JSON schema constraints
- Async, production-style API using FastAPI
- Celery worker task model for scalable background processing
- Redis-backed atomic consumption for request and token quotas
- WebSocket notifications for lifecycle events (`TASK_STARTED`, `LLM_STARTED`, etc.)
- Usage analytics for model provider selection and cost tracking
- Cache support for repeated function-call extraction
- Dead-letter queue support for failed tasks
- CORS, session middleware, and request throttling configured for web apps

## Project setup

### Prerequisites

- Python 3.11+ recommended
- Docker Desktop or Docker Engine
- Redis and PostgreSQL (or Docker Compose)
- Google Cloud OAuth credentials
- A valid Google Gemini API key and/or OpenRouter API key

### 1. Clone and create environment

```bash
git clone <repo-url>
cd <repo-folder>
python -m venv env
# Windows PowerShell
.\env\Scripts\Activate.ps1
# Linux/macOS
source env/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

Create a `.env` file in the project root. Example:

```dotenv
APP__NAME="Calendar Scheduling API"
APP__ENV="development"
APP__DEBUG="false"
APP__LOG_LEVEL="INFO"
APP__API_V1_PREFIX="/api/v1"
APP__DESCRIPTION="Schedule Google Calendar meetings from natural-language requests"

SECURITY__SESSION_SECRET="replace-with-a-long-random-secret"
SECURITY__APP_MASTER_KEY="<64-character-hex-key>"
SECURITY__ALLOW_ORIGINS="http://localhost:3000"
SECURITY__ALLOW_CREDENTIALS="true"
SECURITY__ALLOW_METHODS="*"
SECURITY__ALLOW_HEADERS="*"

DB__ASYNC_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/calendar_service"
DB__SYNC_URL="postgresql+psycopg2://postgres:postgres@localhost:5432/calendar_service"
DB__POOL_SIZE="20"
DB__MAX_OVERFLOW="10"

REDIS__URL="redis://localhost:6379/0"
REDIS__SYNC_DB="0"
REDIS__ASYNC_DB="1"

GOOGLE_OAUTH__CLIENT_ID="your-google-client-id.apps.googleusercontent.com"
GOOGLE_OAUTH__CLIENT_SECRET="your-google-client-secret"
GOOGLE_OAUTH__REDIRECT_URL="http://localhost:8000/auth/callback"

LLM__GEMINI_API_KEY="your-gemini-key"
LLM__OPEN_ROUTER_API_KEY="your-openrouter-key"
```

> Note: the app uses nested settings via `__` env separators. Keep the names aligned with `config/settings.py`.

### 3. Start infrastructure

This project includes a Docker Compose setup for PostgreSQL and Redis:

```bash
docker compose up -d postgres redis
```

### 4. Start the API

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 5. Start the Celery worker

```bash
celery -A celery_app.celery_app worker --loglevel=info -Q calendar
```

If you are using the included `docker-compose.yaml`, the `web` and `celery_worker` services are preconfigured to run together.

### 6. Run the test suite

The repository is organized by test scope:

- `tests/unit/` — isolated unit coverage for routes, tasks, middleware, telemetry, and utilities
- `tests/integration/` — cross-component integration checks
- `tests/performance/` — performance-oriented validation and load-focused checks
- `tests/e2e/` — end-to-end workflow checks against a running environment
- `tests/conftest.py` — shared pytest fixtures and project-wide test setup

To run the unit suite locally:

```bash
# Windows PowerShell
.\env\Scripts\python.exe -m pytest -q tests/unit -m unit

# Linux/macOS
pytest -q tests/unit -m unit
```

This project’s default validation target is the unit suite under `tests/unit`, which covers middleware, tasks, routes, telemetry, and utilities.

## How to use this project

### Authentication

The project uses Google OAuth. Start the flow with:

```http
GET /auth/login
```

This redirects users to Google for consent. The callback is handled at:

```http
GET /auth/callback?code=...&state=...
```

After successful authentication, the user session is stored and the Google tokens are persisted in the database.

### Submit a meeting request

Request a background scheduling task:

```http
POST /api/v1/async-schedule
Content-Type: application/json
```

Example payload:

```json
{
  "request_text": "Schedule a 30-minute product sync tomorrow at 3 PM with alex@example.com and priya@example.com"
}
```

Example response:

```json
{
  "task_id": "3d1c7c7b-6d7e-4f5a-bf1d-f7af6acdc198",
  "status": "PENDING"
}
```

### Check task status

```http
GET /api/v1/tasks/{task_id}
```

Possible task states include `PENDING`, `STARTED`, `SUCCESS`, `FAILURE`, and other Celery lifecycle states.

### Real-time task progress

The app exposes a websocket endpoint for service events. This is useful for progress bars and dashboards:

```text
/ws/events/{task_id}
```

The task emits structured lifecycle events such as:

- `TASK_STARTED`
- `LLM_STARTED`
- `LLM_COMPLETED`
- `GOOGLE_CALENDAR_STARTED`
- `TASK_COMPLETED`
- `TASK_FAILED_DB_ERROR`
- `TASK_DEAD_LETTER`

### Parse a scheduling request without queuing a task

This endpoint can extract the function-call representation from a request directly:

```http
POST /api/v1/parse-event
```

This is useful for validating LLM behavior and caching structured extraction results.

### Admin analytics

Operational insights are available through:

```http
GET /api/v1/admin/llm-analytics
```

This aggregates prompt/completion cost, cache hit rate, provider usage, and latency information.

## Runtime design notes

### Rate limiting

The app uses two safety layers:

- HTTP-level rate limiting via `slowapi`
- Redis-backed token bucket consumption at scheduling time

This protects both the public API and the underlying LLM / Google Calendar costs.

### Task execution model

Calendar scheduling is intentionally asynchronous. Execution is offloaded to a Celery worker so the API remains responsive and can safely handle long-running jobs and retries.

### Dead letter queue

Transient errors are retried; permanent or unresolved failures are pushed to the DLQ and can be inspected via the admin DLQ routes.

### Caching

Repeated scheduling requests can be cached for function-call extraction and repeated processing. This reduces repeated LLM calls and helps control cost.

## Recommended production hardening

This project is a strong foundation, but production deployment should include:

- ✅ TLS termination and secure cookie/session configuration
- Secrets managed through a proper secret store (e.g. environment injection, Vault, Azure Key Vault, AWS Secrets Manager) via config-managed secure values and admin auth hardening
- ✅ Strict CORS policy and explicit trusted origins
- CI/CD pipeline with linting, security scanning, and unit/integration tests (recommended next step for branch protection)
- ✅ DB migrations and rollback strategy for schema changes
- ✅ Monitoring, alerting, and log aggregation for Celery workers and web app
- ✅ User tenancy or multi-tenant isolation for production deployments
- ✅ Additional retry policies and backoff tuning for Google API calls
- ✅ Storage of OAuth refresh/write tokens in a hardened secret store
- ✅ Fine-Grained Tenant RBAC & Security Subsystem 1. Technical Deliverables: Enforce tenant ABAC with cryptographically signed JWTs; establish an append-only audit trail 2. Strategic Operational Impact: Prevents cross-tenant data leaks and ensures complete forensic traceability for DLQ replays
- ✅ Immutable Audit Trail & Structured Event Telemetry Subsystem

1. Signed tenant JWT context and request-time tenant enforcement
2. Tenant-aware DLQ replay validation to stop cross-tenant leakage
3. Centralized append-only audit event writing with HMAC tamper evidence
4. Correlation ID propagation across requests and task execution
5. Structured JSON logging enriched with correlation metadata
6. Audit emission for OAuth refreshes, retry/throttling events, circuit-breaker transitions, and task lifecycle events

- ✅ Event Streaming Bus & Asynchronous Messaging Subsystem

1. Automated backpressure: P95/P99 tracking enables real-time load shedding without manual intervention
2. Zero-downtime tuning: Rate limits and breaker policies can be adjusted live from Redis, no cluster redeploy needed
3. Scalable event consumption: Redis Streams consumer groups allow horizontal scaling of telemetry processors
4. Fine-grained observability: Every policy change is recorded in the immutable audit trail with full correlation context

- ✅ Dynamic Feature Flagging & Anomaly Detection Subsystem

1. Middleware layer: Intercepts all requests, measures latency, triggers anomaly response
2. Circuit breaker: Queries feature flags for dynamic threshold adjustment
3. Rate limiter: Falls back to feature flags for capacity tuning
4. Background jobs: Can be throttled dynamically via feature flags
5. Audit trail: All policy changes logged with correlation IDs

## TODO / roadmap

- Add unit and integration tests for routes, Celery tasks, and schema validation
- ✅ Add API versioning documentation and OpenAPI examples for production clients
- ✅ Improve error taxonomy for transient vs permanent scheduling failures
- ✅ Add a true admin dashboard for task health and DLQ events
- ✅ Add background health checks for Redis, Postgres, Google APIs, and Celery workers
- ✅ Add structured observability with correlation IDs and per-request tracing
- ✅ Add user-level quotas and tenant-based authorization controls
- ✅ Add event recurrence support and broader calendar editing workflows
- ✅ Add idempotency keys for scheduling requests to avoid duplicate meeting creation
- ✅ Add webhook or callback support for external systems
- ✅ Add migrations/seed scripts for easier local setup and staging deployment
- ✅ Add timezone-aware scheduling prompts and DST-safe wall-clock normalization
- ✅ Add safe DLQ replay, quarantine, and audit controls
- ✅ Add dynamic throttle reduction using Redis multiplier state
- ✅ Add OAuth token rotation with Redis grace-window and token-family quarantine
- Deploy to test environment and verify Pub/Sub flag updates propagate to running workers
- Simulate latency spikes to confirm automatic circuit breaker half-open triggering
- Add monitoring dashboard for real-time percentile visualization
- Consider advanced anomaly models (seasonal decomposition, ML-based detection)
