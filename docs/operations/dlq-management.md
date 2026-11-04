# Dead-Letter Queue & Incident Quarantine

When background tasks encounter non-retryable errors or exhaust max retry attempts, they enter the Dead-Letter Queue (DLQ).

---

## Triage & Incident Workflow

```mermaid
graph TD
    Failure[Task Execution Failure] --> CheckCount{Retry Count >= 3?}
    CheckCount -->|No| Retry[Exponential Backoff Retry]
    CheckCount -->|Yes| DLQ[Move to DLQ Storage]
    
    DLQ --> AdminInspect[Admin Inspects /admin/dlq/]
    AdminInspect --> DryRun[Run POST /admin/dlq/dry-run]
    
    DryRun -->|Valid Payload| Replay[POST /admin/dlq/replay]
    DryRun -->|Corrupted Payload| Quarantine[Move to Permanent Quarantine]
    
    Replay --> CheckReplayCount{Replay Attempts >= 3?}
    CheckReplayCount -->|Pass| Success[Task Completed]
    CheckReplayCount -->|Fail| Quarantine
```

---

## 3-Retry Quarantine Rule

To protect upstream APIs from poison-pill payloads:
* Tasks attempting more than **3 manual replays** are automatically locked into **Quarantine**.
* Quarantined tasks cannot be replayed without explicit database state updates by system maintainers.
