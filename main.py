# main.py

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from fastapi.middleware.cors import CORSMiddleware
import logging

from config.settings import settings
from config.limiter import limiter
from config.logging_config import setup_logging
from config.database import close_db

from routes.auth import router as auth_router
from routes.event_parser import event_parser_router
from routes.meeting_scheduler import router as meeting_scheduler_router
from routes.task_status import router as task_status_router
from routes.websocket_events import router as websocket_router
from routes.admin_analytics import router as admin_analytics_router
from routes.dlq_admin import router as dlq_admin_router
from routes.feature_flags_admin import router as feature_flags_router
from routes.monitoring_dashboard import router as monitoring_router
from routes.tenant_resources import router as tenant_resources_router

from middleware.tenant_context import TenantContextMiddleware
from middleware.request_correlation import RequestCorrelationMiddleware
from middleware.anomaly_detection import AnomalyDetectionMiddleware
from utilities.feature_flags import initialize_feature_flags
from telemetry.manager import get_shared_ledger, stop_shared_ledger
from config.cache import get_redis_client
from messaging.bus import AsyncMessageBus
from flags.engine import IntegratedFlagAnomalyManager

# Initialize global logging before creating the app
setup_logging(log_level=settings.app.log_level, environment=settings.app.env)
# Initialize dynamic feature flag system
initialize_feature_flags()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Run any startup tasks (e.g., warm up Redis cache)
    try:
        ledger = get_shared_ledger()
        app.state.audit_ledger = ledger
    except Exception:
        logging.getLogger(__name__).exception(
            "Failed to initialize shared audit ledger"
        )

    # Initialize AsyncMessageBus for idempotent request processing
    redis_client = None
    try:
        redis_client = await get_redis_client()
        message_bus = AsyncMessageBus(redis_client=redis_client, ttl_seconds=3600)
        app.state.message_bus = message_bus
        logging.getLogger(__name__).debug("Initialized AsyncMessageBus")
    except Exception:
        logging.getLogger(__name__).exception("Failed to initialize message bus")

    # Initialize IntegratedFlagAnomalyManager for feature flags and anomaly detection
    try:
        if redis_client is None:
            redis_client = await get_redis_client()
        flag_anomaly_mgr = IntegratedFlagAnomalyManager(
            redis_client=redis_client,
            anomaly_health_threshold=70.0,
            nominal_rate_limit=settings.rate_limit.requests_per_minute or 100.0,
        )
        await flag_anomaly_mgr.start()
        app.state.flag_anomaly_manager = flag_anomaly_mgr
        logging.getLogger(__name__).debug("Initialized IntegratedFlagAnomalyManager")
    except Exception:
        logging.getLogger(__name__).exception(
            "Failed to initialize flag/anomaly manager"
        )

    yield
    # Shutdown: Clean up connection pools gracefully
    await close_db()
    try:
        stop_shared_ledger()
    except Exception:
        logging.getLogger(__name__).exception("Failed to stop shared audit ledger")
    try:
        flag_anomaly_mgr = getattr(app.state, "flag_anomaly_manager", None)
        if flag_anomaly_mgr:
            await flag_anomaly_mgr.stop()
    except Exception:
        logging.getLogger(__name__).exception("Failed to stop flag/anomaly manager")


app = FastAPI(
    title=settings.app.name,
    version=settings.app.api_v1_prefix,
    description=settings.app.description,
    lifespan=lifespan,
)

# Attach Limiter State & Error Handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.security.session_secret,
    https_only=(settings.app.env == "production"),
    same_site="lax",
)

app.add_middleware(
    CORSMiddleware,
    # Convert Pydantic AnyHttpUrl objects to strings for Middleware
    allow_origins=[str(origin) for origin in settings.security.allow_origins],
    allow_credentials=settings.security.allow_credentials,
    allow_methods=settings.security.allow_methods,
    allow_headers=settings.security.allow_headers,
)

app.add_middleware(TenantContextMiddleware)
app.add_middleware(RequestCorrelationMiddleware)
app.add_middleware(AnomalyDetectionMiddleware)

app.include_router(auth_router)
app.include_router(event_parser_router)
app.include_router(meeting_scheduler_router)
app.include_router(task_status_router)
app.include_router(websocket_router)
app.include_router(admin_analytics_router)
app.include_router(dlq_admin_router)
app.include_router(feature_flags_router)
app.include_router(monitoring_router)
app.include_router(tenant_resources_router)
