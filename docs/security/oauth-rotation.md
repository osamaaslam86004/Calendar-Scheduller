# OAuth Token Family Rotation & Grace Period

To secure Google Calendar access tokens without race conditions during concurrent worker execution, the system implements an atomic **OAuth Token Manager** (`tasks/refresh_oauth_tokens.py`).

---

## Distributed Lock & Refresh Protocol

When a background worker detects an expired or near-expired Google OAuth token:

```mermaid
sequenceDiagram
    autonumber
    participant Worker 1
    participant Worker 2
    participant Redis Lock
    participant OAuthMgr as OAuth Token Manager
    participant Provider as Google OAuth API

    Worker 1->>OAuthMgr: Request Valid Access Token
    Worker 2->>OAuthMgr: Request Valid Access Token (Simultaneous)
    
    OAuthMgr->>Redis Lock: Acquire lock:oauth:refresh:{user_id}
    Redis Lock-->>Worker 1: Lock Acquired
    Redis Lock-->>Worker 2: Lock Busy (Wait)

    Worker 1->>Provider: Refresh OAuth Credentials
    Provider-->>Worker 1: New Refresh & Access Tokens
    
    Worker 1->>Redis Lock: Store in Grace Window (oauth:grace:{token}, TTL: 45s)
    Worker 1->>Redis Lock: Update Token Family Set (oauth:token_family:{user_id})
    Worker 1->>Redis Lock: Release Lock
    
    Redis Lock-->>Worker 2: Lock Released
    Worker 2->>Redis Lock: Read Grace Window Key
    Redis Lock-->>Worker 2: Return Cached Fresh Access Token
```

---

## Grace Period & Compromise Quarantine

### 1. 45-Second Grace Window
When multiple workers refresh a user's token simultaneously, the first worker writes the new token pair to `oauth:grace:{old_refresh_token}` in Redis with a 45-second expiry. Subsequent workers obtain the fresh access token directly from Redis, preventing redundant OAuth token revocations.

### 2. Reuse & Compromise Detection
Every issued refresh token is tracked in a Redis family lineage set (`oauth:token_family:{user_id}`).

!!! failure "Token Reuse Alert"
    If an old, previously rotated refresh token is presented outside the 45-second grace window:
    1. The manager flags an `OAuthCompromiseException`.
    2. The entire token family set is quarantined and moved to `oauth:revoked_tokens:{user_id}`.
    3. User database credentials are immediately invalidated.
    4. An audit ledger event `OAUTH_FAMILY_COMPROMISE_REVOCATION` is emitted.
