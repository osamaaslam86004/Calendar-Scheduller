import time
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytest.importorskip("middleware.anomaly_detection")
from middleware.anomaly_detection import AnomalyDetectionMiddleware  # type: ignore


@pytest.mark.unit
def test_response_time_header_present_and_positive():
    app = FastAPI()

    @app.get("/ping")
    def ping():
        time.sleep(0.01)
        return {"ok": True}

    app.add_middleware(AnomalyDetectionMiddleware)

    client = TestClient(app)
    resp = client.get("/ping")
    assert resp.status_code == 200
    assert "X-Response-Time-Ms" in resp.headers
    val = float(resp.headers["X-Response-Time-Ms"])
    assert val >= 0.0


@pytest.mark.unit
def test_high_latency_can_be_measured_and_reported():
    app = FastAPI()

    @app.get("/slow")
    def slow():
        time.sleep(0.2)
        return {"ok": True}

    # Add middleware without implementation-specific kwargs to remain compatible
    app.add_middleware(AnomalyDetectionMiddleware)

    client = TestClient(app)
    resp = client.get("/slow")
    assert resp.status_code == 200
    assert "X-Response-Time-Ms" in resp.headers
