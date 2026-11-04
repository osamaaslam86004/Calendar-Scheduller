# Calendar Scheduling API

Welcome to the official developer documentation for the **Natural Language Calendar Scheduling API**, a production-oriented FastAPI backend designed to translate human natural-language scheduling requests into structured Google Calendar appointments with integrated Google Meet conference links.

!!! info "Production Infrastructure Overview"
    This service is built for high availability, zero-downtime policy adjustments, cryptographically verifiable audit logging, multi-tenant RBAC, and resilient asynchronous worker processing.

---

## Core Capabilities

=== "Natural Language Scheduling"
    * Extracts event title, start/end times, attendees, duration, and recurrence rules from unformatted text prompts.
    * Dual-provider fallback chain (Google Gemini API with OpenRouter secondary fallback).
    * Validates LLM function-call output against strict Pydantic contract schemas (`schemas.py`).

=== "Asynchronous Architecture"
    * Decouples HTTP request ingestion from Google Calendar execution using **Celery** workers and **Redis**.
    * Real-time task progress streaming over **WebSockets** (`/ws/events/{task_id}`).
    * Dual-bucket Redis-backed rate limiting protects against upstream API abuse and unexpected token expenses.

=== "Enterprise Security & Isolation"
    * Multi-tenant validation via cryptographically signed JWTs (`X-Tenant-Context`).
    * Distributed request tracing via correlation ID headers (`X-Correlation-ID`).
    * OAuth token family rotation with Redis distributed locks, a 45-second grace window, and compromise quarantine (`tasks/refresh_oauth_tokens.py`).

=== "Resilience & Telemetry"
    * Integrated Anomaly Detection Engine monitoring P95 latency, error rates, CPU/memory utilization, and queue depth (`flags/engine.py`).
    * Immutable, append-only HMAC-SHA256 audit ledger with automated PII masking (`telemetry/audit_ledger.py`).
    * Idempotent Message Bus hashing SHA-256 payload fingerprints to block payload tampering (`messaging/bus.py`).
    * Admin DLQ router supporting dry-run verification, HMAC signing, safe replay, and 3-retry quarantine (`routes/dlq_admin.py`).

---

## Technical Stack Requirements

```yaml
Dependencies:
  mkdocs: ">=1.6.1"
  mkdocs-material: ">=9.8.0"
  mkdocs-autorefs: ">=1.4.5"
  mkdocs-panzoom-plugin: ">=0.5.3"
  pymdown-extensions: ">=12.3"
```

---

## Quick Navigation

* [Architecture & Topology Overview](architecture/overview.md)
* [Security & Tenant Governance](security/tenant-rbac.md)
* [API Reference & Endpoint Contracts](api/scheduling.md)
* [Celery Background Workers](operations/celery-tasks.md)
* [Local Setup & Testing Guide](dev/setup.md)
