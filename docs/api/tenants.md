# API Reference: Multi-Tenant Resources

Endpoints for tenant organizational context inspection and resource boundary enforcement (`routes/tenant_resources.py`).

---

## 1. Inspect Tenant Info

```http
GET /api/v1/tenant-resources/
```

Returns identity parameters, role assignments, and permission boundaries extracted from the validated `X-Tenant-Context` JWT.

### Request Headers

| Header | Type | Requirement | Description |
| :--- | :--- | :--- | :--- |
| `X-Tenant-Context` | `String (JWT)` | **Required** | Signed tenant authorization claims token. |

### Response (`200 OK`)

```json
{
  "tenant_id": "tenant_enterprise_99",
  "user_id": 4821,
  "roles": ["org_admin", "scheduler_user"],
  "bounds": {
    "allowed_operations": ["schedule:create", "schedule:read", "dlq:replay"],
    "max_attendees_per_event": 50
  }
}
```

---

## 2. Perform Tenant Action

```http
POST /api/v1/tenant-resources/action
```

Executes a tenant-isolated administrative action within the boundary of the tenant's context.

### Request Body

```json
{
  "action": "purge_cached_schedules",
  "resource_id": "res_8812"
}
```

### Response (`200 OK`)

```json
{
  "status": "SUCCESS",
  "action": "purge_cached_schedules",
  "executed_at": "2026-11-04T03:15:00Z"
}
```
