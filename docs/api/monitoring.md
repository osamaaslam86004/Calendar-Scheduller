# API Reference: Monitoring & Metrics

Endpoints exposing operational metrics, percentile latency snapshots, and the live frontend dashboard (`routes/monitoring_dashboard.py`).

---

## 1. System Health Snapshot

```http
GET /api/v1/monitoring/health-snapshot
```

### Response (`200 OK`)

```json
{
  "timestamp": "2026-11-04T03:20:00Z",
  "health_score": 94.5,
  "total_anomalies": 0,
  "operations_monitored": ["POST /api/v1/async-schedule"],
  "metrics": {
    "POST /api/v1/async-schedule": {
      "p50": 120.5,
      "p95": 410.2,
      "p99": 890.0,
      "mean": 185.0,
      "sample_count": 1420,
      "is_anomaly": false
    }
  },
  "circuit_breaker_state": {
    "google_calendar_api": "CLOSED"
  }
}
```

---

## 2. Interactive Dashboard HTML

```http
GET /api/v1/monitoring/dashboard-html
```

Serves an interactive Chart.js HTML web application visualizing latency percentiles, active anomaly alerts, and dynamic flag controls.
