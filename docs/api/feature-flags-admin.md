# API Reference: Feature Flags & Controls

Administrative routes for dynamic feature flag updates, rate-limit capacity scaling, and circuit breaker overrides (`routes/feature_flags_admin.py`).

---

## 1. Set Feature Flag

```http
POST /api/v1/admin/flags/set
```

### Request Body

```json
{
  "flag_name": "enable_openrouter_fallback",
  "value": true
}
```

---

## 2. Scale Rate Limit Capacity

```http
POST /api/v1/admin/flags/rate-limit/set
```

### Request Body

```json
{
  "capacity": 250
}
```

---

## 3. Update Circuit Breaker Threshold

```http
POST /api/v1/admin/flags/circuit-breaker/set
```

### Request Body

```json
{
  "failure_threshold": 5.0,
  "recovery_time_seconds": 30
}
```

---

## 4. Throttle Background Jobs

```http
POST /api/v1/admin/flags/background-job/throttle
```

### Request Body

```json
{
  "max_concurrent_tasks": 10
}
```
