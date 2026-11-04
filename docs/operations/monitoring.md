# Monitoring & Observability Operations

Operational procedures for evaluating system health metrics and operating the Chart.js dashboard.

---

## Dashboard Capabilities (`/api/v1/monitoring/dashboard-html`)

1. **Real-time Percentile Tracking**: Displays P50, P95, and P99 latency charts updated every 5 seconds.
2. **Anomaly Alert Banners**: Highlights active anomaly conditions detected by `AnomalyEngine`.
3. **Policy Tuning Controls**: Live adjustment controls for rate-limiter capacities and circuit breaker sensitivity.

---

## Metric Alerting Thresholds

```text
ALERT: P95 Latency > 5000ms for 3 consecutive intervals -> Trigger Half-Open Circuit Breaker
ALERT: Error Rate > 10.0% -> Auto-reduce Rate Limit Capacity by 50%
ALERT: Composite Health Score < 70.0 -> Escalate to On-Call Systems Architect
```
