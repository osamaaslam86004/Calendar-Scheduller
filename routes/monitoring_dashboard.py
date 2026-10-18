"""Real-time monitoring dashboard API endpoints for percentile visualization and metrics."""

import json
from datetime import datetime, timedelta
from typing import Optional

import redis
from fastapi import APIRouter, HTTPException, Query

from config.settings import settings
from utilities.anomaly_detector import LatencyPercentileTracker
from utilities.feature_flags import (
    get_circuit_breaker_threshold,
    get_rate_limit_capacity,
    get_oauth_grace_period_hours,
)
from schemas import MetricsHistoryPoint, PercentileMetrics, SystemHealthSnapshot
from utilities.security import get_current_correlation_id

router = APIRouter(prefix="/api/v1/monitoring", tags=["monitoring"])
_redis = redis.Redis.from_url(settings.redis.url, decode_responses=True)


@router.get("/health-snapshot", response_model=SystemHealthSnapshot)
async def get_health_snapshot():
    """
    Get a current snapshot of system health with all monitored operations.

    Returns percentiles, anomaly status, and circuit breaker state.
    """
    # Get all monitored operations from Redis keys
    pattern = "latency:percentiles:*"
    keys = _redis.keys(pattern)

    if not keys:
        return SystemHealthSnapshot(
            timestamp=datetime.utcnow().isoformat(),
            total_anomalies=0,
            operations_monitored=[],
            metrics={},
            circuit_breaker_state={},
        )

    metrics = {}
    total_anomalies = 0

    for key in keys:
        operation = key.replace("latency:percentiles:", "")
        data = _redis.get(key)
        if not data:
            continue

        try:
            percentiles = json.loads(data)
            if percentiles.get("is_anomaly"):
                total_anomalies += 1

            metrics[operation] = PercentileMetrics(
                operation=operation,
                p50=percentiles.get("p50", 0.0),
                p95=percentiles.get("p95", 0.0),
                p99=percentiles.get("p99", 0.0),
                mean=percentiles.get("mean", 0.0),
                std_dev=percentiles.get("std_dev", 0.0),
                sample_count=percentiles.get("sample_count", 0),
                anomaly_threshold=percentiles.get("anomaly_threshold", 0.0),
                is_anomaly=percentiles.get("is_anomaly", False),
                last_updated=datetime.utcnow().isoformat(),
            )
        except (json.JSONDecodeError, TypeError):
            continue

    # Get circuit breaker states (mock for now, would integrate with actual circuit breaker)
    circuit_breaker_state = {
        "llm_provider": _redis.get("circuit_breaker:llm_provider:state") or "CLOSED",
        "google_calendar": _redis.get("circuit_breaker:google_calendar:state")
        or "CLOSED",
    }

    return SystemHealthSnapshot(
        timestamp=datetime.utcnow().isoformat(),
        total_anomalies=total_anomalies,
        operations_monitored=list(metrics.keys()),
        metrics=metrics,
        circuit_breaker_state=circuit_breaker_state,
    )


@router.get("/percentiles/{operation}", response_model=Optional[PercentileMetrics])
async def get_percentiles_for_operation(operation: str):
    """
    Get current percentile metrics for a specific operation.

    Args:
        operation: The operation name to query (e.g., 'schedule_meeting', 'fetch_calendar')
    """
    tracker = LatencyPercentileTracker(operation)
    percentiles = tracker.from_redis(operation)

    if not percentiles:
        raise HTTPException(status_code=404, detail=f"No metrics found for {operation}")

    return PercentileMetrics(
        operation=operation,
        p50=percentiles.get("p50", 0.0),
        p95=percentiles.get("p95", 0.0),
        p99=percentiles.get("p99", 0.0),
        mean=percentiles.get("mean", 0.0),
        std_dev=percentiles.get("std_dev", 0.0),
        sample_count=percentiles.get("sample_count", 0),
        anomaly_threshold=percentiles.get("anomaly_threshold", 0.0),
        is_anomaly=percentiles.get("is_anomaly", False),
        last_updated=datetime.utcnow().isoformat(),
    )


@router.get("/anomalies", response_model=list[PercentileMetrics])
async def get_current_anomalies():
    """
    Get all operations currently in anomalous state.

    Returns list of operations with P95 exceeding anomaly threshold.
    """
    pattern = "latency:percentiles:*"
    keys = _redis.keys(pattern)
    anomalies = []

    for key in keys:
        data = _redis.get(key)
        if not data:
            continue

        try:
            percentiles = json.loads(data)
            if percentiles.get("is_anomaly"):
                operation = key.replace("latency:percentiles:", "")
                anomalies.append(
                    PercentileMetrics(
                        operation=operation,
                        p50=percentiles.get("p50", 0.0),
                        p95=percentiles.get("p95", 0.0),
                        p99=percentiles.get("p99", 0.0),
                        mean=percentiles.get("mean", 0.0),
                        std_dev=percentiles.get("std_dev", 0.0),
                        sample_count=percentiles.get("sample_count", 0),
                        anomaly_threshold=percentiles.get("anomaly_threshold", 0.0),
                        is_anomaly=True,
                        last_updated=datetime.utcnow().isoformat(),
                    )
                )
        except (json.JSONDecodeError, TypeError):
            continue

    return anomalies


@router.get("/policy-state")
async def get_current_policy_state():
    """
    Get current state of all dynamic policies for inspection.

    Returns rate limits, circuit breaker thresholds, token refill rates, and OAuth grace periods.
    """
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "rate_limits": {
            "default": get_rate_limit_capacity("default"),
            "openai": get_rate_limit_capacity("openai"),
            "google_calendar": get_rate_limit_capacity("google_calendar"),
        },
        "circuit_breaker_thresholds": {
            "llm_provider": get_circuit_breaker_threshold("llm_provider"),
            "google_calendar": get_circuit_breaker_threshold("google_calendar"),
        },
        "token_refill_rates": {
            "default": _redis.get("token:refill_rate:default") or "60",
            "premium": _redis.get("token:refill_rate:premium") or "120",
        },
        "oauth_grace_period": get_oauth_grace_period_hours(),
    }


@router.get("/metrics-history/{operation}")
async def get_metrics_history(
    operation: str,
    time_window_minutes: int = Query(default=60, ge=1, le=1440),
) -> list[MetricsHistoryPoint]:
    """
    Get historical percentile metrics for an operation over a time window.

    Retrieves snapshots stored in Redis stream for charting.

    Args:
        operation: The operation name
        time_window_minutes: Number of minutes to look back (default: 60, max: 1440)
    """
    stream_key = f"metrics:history:{operation}"

    # Get all entries from stream (Redis streams store time-ordered data)
    entries = _redis.xrange(stream_key)

    if not entries:
        raise HTTPException(status_code=404, detail=f"No history found for {operation}")

    history = []
    cutoff_time = datetime.utcnow() - timedelta(minutes=time_window_minutes)

    for entry_id, data in entries:
        try:
            timestamp = datetime.fromisoformat(
                data.get("timestamp", datetime.utcnow().isoformat())
            )
            if timestamp < cutoff_time:
                continue

            history.append(
                MetricsHistoryPoint(
                    timestamp=data.get("timestamp", datetime.utcnow().isoformat()),
                    p50=float(data.get("p50", 0.0)),
                    p95=float(data.get("p95", 0.0)),
                    p99=float(data.get("p99", 0.0)),
                    mean=float(data.get("mean", 0.0)),
                    is_anomaly=data.get("is_anomaly", "false").lower() == "true",
                )
            )
        except (ValueError, TypeError):
            continue

    return sorted(history, key=lambda x: x.timestamp)


@router.get("/dashboard-html")
async def get_dashboard_html():
    """
    Serve the real-time monitoring dashboard HTML.

    Returns the interactive dashboard with chart.js integration for percentile visualization.
    """
    from fastapi.responses import HTMLResponse

    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Latency Monitoring Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/axios/dist/axios.min.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #1e1e2e 0%, #2a2a3e 100%);
            color: #e0e0e0;
            min-height: 100vh;
            padding: 20px;
        }
        
        .container {
            max-width: 1600px;
            margin: 0 auto;
        }
        
        header {
            margin-bottom: 30px;
            padding: 20px;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 8px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        
        h1 {
            font-size: 28px;
            margin-bottom: 10px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .status-line {
            display: flex;
            gap: 30px;
            margin-top: 15px;
            font-size: 14px;
        }
        
        .status-item {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        
        .status-badge {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: #4ade80;
            animation: pulse 2s infinite;
        }
        
        .status-badge.anomaly {
            background: #ef4444;
            animation: pulse-red 1s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        
        @keyframes pulse-red {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
        
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(500px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .card {
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            padding: 20px;
            backdrop-filter: blur(10px);
            transition: all 0.3s ease;
        }
        
        .card:hover {
            background: rgba(255, 255, 255, 0.12);
            border-color: rgba(255, 255, 255, 0.2);
            transform: translateY(-2px);
        }
        
        .card-title {
            font-size: 16px;
            font-weight: 600;
            margin-bottom: 15px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .metric-row {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 15px;
            margin-bottom: 10px;
        }
        
        .metric {
            padding: 10px;
            background: rgba(0, 0, 0, 0.2);
            border-radius: 6px;
            border-left: 3px solid #667eea;
        }
        
        .metric-label {
            font-size: 12px;
            color: #a0a0a0;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .metric-value {
            font-size: 20px;
            font-weight: 600;
            margin-top: 5px;
            color: #e0e0e0;
        }
        
        .metric-unit {
            font-size: 12px;
            color: #808080;
            margin-left: 5px;
        }
        
        .anomaly-badge {
            background: #ef4444;
            color: white;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
        }
        
        .healthy-badge {
            background: #4ade80;
            color: white;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
        }
        
        .chart-container {
            grid-column: 1 / -1;
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            padding: 20px;
            backdrop-filter: blur(10px);
        }
        
        .chart-wrapper {
            position: relative;
            height: 400px;
        }
        
        .controls {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        
        button {
            padding: 8px 16px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border: none;
            color: white;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.3s ease;
        }
        
        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 16px rgba(102, 126, 234, 0.4);
        }
        
        button:active {
            transform: translateY(0);
        }
        
        select {
            padding: 8px 12px;
            background: rgba(255, 255, 255, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.2);
            color: #e0e0e0;
            border-radius: 6px;
            font-size: 14px;
            cursor: pointer;
        }
        
        .anomalies-list {
            max-height: 300px;
            overflow-y: auto;
        }
        
        .anomaly-item {
            padding: 10px;
            background: rgba(239, 68, 68, 0.1);
            border-left: 3px solid #ef4444;
            margin-bottom: 8px;
            border-radius: 4px;
        }
        
        .footer {
            text-align: center;
            padding: 20px;
            color: #606080;
            font-size: 12px;
        }
        
        .loading {
            text-align: center;
            padding: 40px;
            color: #a0a0a0;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>📊 Latency Monitoring Dashboard</h1>
            <div class="status-line">
                <div class="status-item">
                    <div class="status-badge" id="statusBadge"></div>
                    <span id="statusText">Initializing...</span>
                </div>
                <div class="status-item">
                    <span>Operations Monitored: <strong id="operationCount">0</strong></span>
                </div>
                <div class="status-item">
                    <span>Anomalies Active: <strong id="anomalyCount">0</strong></span>
                </div>
                <div class="status-item">
                    <span>Last Update: <strong id="lastUpdate">-</strong></span>
                </div>
            </div>
        </header>

        <div class="controls">
            <button onclick="refreshDashboard()">🔄 Refresh Now</button>
            <select id="operationSelect" onchange="updateCharts()">
                <option value="">Select Operation...</option>
            </select>
            <button onclick="startAutoRefresh()">▶ Auto Refresh (5s)</button>
            <button onclick="stopAutoRefresh()">⏸ Stop Auto Refresh</button>
        </div>

        <div class="grid" id="metricsGrid">
            <div class="loading">Loading metrics...</div>
        </div>

        <div class="chart-container">
            <h3 style="margin-bottom: 15px;">Latency Trends</h3>
            <div class="chart-wrapper">
                <canvas id="percentileChart"></canvas>
            </div>
        </div>

        <div class="grid" style="margin-top: 30px">
            <div class="card">
                <div class="card-title">Active Anomalies</div>
                <div class="anomalies-list" id="anomaliesList">
                    <div style="color: #808080">No anomalies detected</div>
                </div>
            </div>
            <div class="card">
                <div class="card-title">Policy Configuration</div>
                <div id="policyConfig" style="font-size: 13px">
                    <div style="color: #a0a0a0">Loading policy state...</div>
                </div>
            </div>
        </div>
        
        <div class="footer">
            Real-time percentile monitoring • Updates automatically every 5 seconds
        </div>
    </div>

    <script>
        let percentileChart = null;
        let autoRefreshInterval = null;
        const API_BASE = '/api/v1/monitoring';

        async function fetchHealthSnapshot() {
            try {
                const response = await axios.get(`${API_BASE}/health-snapshot`);
                return response.data;
            } catch (error) {
                console.error('Failed to fetch health snapshot:', error);
                return null;
            }
        }

        async function fetchPolicyState() {
            try {
                const response = await axios.get(`${API_BASE}/policy-state`);
                return response.data;
            } catch (error) {
                console.error('Failed to fetch policy state:', error);
                return null;
            }
        }

        async function fetchAnomalies() {
            try {
                const response = await axios.get(`${API_BASE}/anomalies`);
                return response.data;
            } catch (error) {
                console.error('Failed to fetch anomalies:', error);
                return [];
            }
        }

        async function refreshDashboard() {
            const snapshot = await fetchHealthSnapshot();
            if (!snapshot) return;

            // Update header
            const anomalyCount = snapshot.total_anomalies;
            const statusBadge = document.getElementById('statusBadge');
            const statusText = document.getElementById('statusText');
            
            if (anomalyCount > 0) {
                statusBadge.classList.add('anomaly');
                statusText.textContent = `⚠️ ${anomalyCount} Anomaly(ies) Detected`;
            } else {
                statusBadge.classList.remove('anomaly');
                statusText.textContent = '✅ System Healthy';
            }

            document.getElementById('operationCount').textContent = snapshot.operations_monitored.length;
            document.getElementById('anomalyCount').textContent = anomalyCount;
            document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();

            // Update operations select
            const select = document.getElementById('operationSelect');
            const currentValue = select.value;
            select.innerHTML = '<option value="">Select Operation...</option>';
            snapshot.operations_monitored.forEach(op => {
                const option = document.createElement('option');
                option.value = op;
                option.textContent = op;
                select.appendChild(option);
            });
            if (currentValue) select.value = currentValue;

            // Update metrics grid
            const grid = document.getElementById('metricsGrid');
            grid.innerHTML = '';
            
            snapshot.operations_monitored.forEach(operation => {
                const metrics = snapshot.metrics[operation];
                const isAnomaly = metrics.is_anomaly;
                const card = document.createElement('div');
                card.className = 'card';
                card.innerHTML = `
                    <div class="card-title">
                        <span>${operation}</span>
                        ${isAnomaly ? '<span class="anomaly-badge">ANOMALY</span>' : '<span class="healthy-badge">HEALTHY</span>'}
                    </div>
                    <div class="metric-row">
                        <div class="metric">
                            <div class="metric-label">P50 Latency</div>
                            <div class="metric-value">${metrics.p50}<span class="metric-unit">ms</span></div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">P95 Latency</div>
                            <div class="metric-value">${metrics.p95}<span class="metric-unit">ms</span></div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">P99 Latency</div>
                            <div class="metric-value">${metrics.p99}<span class="metric-unit">ms</span></div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">Mean±σ</div>
                            <div class="metric-value">${metrics.mean}<span class="metric-unit">ms</span></div>
                        </div>
                    </div>
                    <div class="metric">
                        <div class="metric-label">Samples</div>
                        <div class="metric-value">${metrics.sample_count}</div>
                    </div>
                `;
                grid.appendChild(card);
            });

            // Update anomalies list
            const anomalies = await fetchAnomalies();
            const anomaliesList = document.getElementById('anomaliesList');
            if (anomalies.length > 0) {
                anomaliesList.innerHTML = anomalies.map(a => `
                    <div class="anomaly-item">
                        <strong>${a.operation}</strong><br>
                        P95: ${a.p95}ms (threshold: ${a.anomaly_threshold}ms)
                    </div>
                `).join('');
            } else {
                anomaliesList.innerHTML = '<div style="color: #808080">No anomalies detected</div>';
            }

            // Update policy state
            const policyState = await fetchPolicyState();
            if (policyState) {
                const policyConfig = document.getElementById('policyConfig');
                policyConfig.innerHTML = `
                    <div style="margin-bottom: 10px;">
                        <strong>Rate Limits:</strong><br>
                        Default: ${policyState.rate_limits.default}/min
                    </div>
                    <div style="margin-bottom: 10px;">
                        <strong>Circuit Breakers:</strong><br>
                        LLM: ${policyState.circuit_breaker_thresholds.llm_provider}
                    </div>
                    <div>
                        <strong>OAuth Grace:</strong><br>
                        ${policyState.oauth_grace_period} hours
                    </div>
                `;
            }
        }

        async function updateCharts() {
            const operation = document.getElementById('operationSelect').value;
            if (!operation) return;

            try {
                const response = await axios.get(`${API_BASE}/percentiles/${operation}`);
                const metrics = response.data;

                const ctx = document.getElementById('percentileChart').getContext('2d');
                
                if (percentileChart) {
                    percentileChart.destroy();
                }

                percentileChart = new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: ['P50', 'Mean', 'P95', 'Threshold', 'P99'],
                        datasets: [{
                            label: `${operation} Latency`,
                            data: [
                                metrics.p50,
                                metrics.mean,
                                metrics.p95,
                                metrics.anomaly_threshold,
                                metrics.p99
                            ],
                            borderColor: metrics.is_anomaly ? '#ef4444' : '#667eea',
                            backgroundColor: metrics.is_anomaly 
                                ? 'rgba(239, 68, 68, 0.1)' 
                                : 'rgba(102, 126, 234, 0.1)',
                            borderWidth: 2,
                            fill: true,
                            tension: 0.4,
                            pointRadius: 6,
                            pointBackgroundColor: metrics.is_anomaly ? '#ef4444' : '#667eea',
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: {
                                labels: { color: '#e0e0e0' }
                            }
                        },
                        scales: {
                            y: {
                                beginAtZero: true,
                                ticks: { color: '#a0a0a0' },
                                grid: { color: 'rgba(255, 255, 255, 0.05)' }
                            },
                            x: {
                                ticks: { color: '#a0a0a0' },
                                grid: { color: 'rgba(255, 255, 255, 0.05)' }
                            }
                        }
                    }
                });
            } catch (error) {
                console.error('Failed to update charts:', error);
            }
        }

        function startAutoRefresh() {
            if (autoRefreshInterval) return;
            autoRefreshInterval = setInterval(() => {
                refreshDashboard();
                updateCharts();
            }, 5000);
            alert('Auto-refresh started (5s interval)');
        }

        function stopAutoRefresh() {
            if (autoRefreshInterval) {
                clearInterval(autoRefreshInterval);
                autoRefreshInterval = null;
                alert('Auto-refresh stopped');
            }
        }

        // Initial load
        refreshDashboard();
    </script>
</body>
</html>"""

    return HTMLResponse(content=html_content)
