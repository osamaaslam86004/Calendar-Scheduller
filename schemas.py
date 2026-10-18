from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, EmailStr, Field


class SendUpdatesOption(str, Enum):
    ALL = "all"
    EXTERNAL_ONLY = "externalOnly"
    NONE = "none"


class CalendarEventDateTime(BaseModel):
    date_time: str = Field(
        ...,
        description="Start or end time in ISO 8601 format (e.g. '2026-08-20T14:00:00Z')",
    )
    time_zone: str = Field(
        default="UTC",
        description="Timezone identifier, e.g., 'America/New_York' or 'UTC'",
    )


class ScheduleCalendarEventFunction(BaseModel):
    """Function calling schema for creating a Google Calendar meeting event."""

    summary: str = Field(
        ...,
        min_length=3,
        max_length=200,
        description="Title or summary of the meeting event",
    )
    description: Optional[str] = Field(
        default="", description="Detailed agenda or notes for the meeting"
    )
    location: Optional[str] = Field(
        default="", description="Physical location or video call link"
    )
    start: CalendarEventDateTime = Field(..., description="Meeting start date and time")
    end: CalendarEventDateTime = Field(..., description="Meeting end date and time")
    attendees: List[EmailStr] = Field(
        default_factory=list, description="List of participant email addresses"
    )
    send_updates: SendUpdatesOption = Field(
        default=SendUpdatesOption.ALL,
        description="Notification setting for participants",
    )


class UserScheduleRequest(BaseModel):
    request_text: str = Field(
        ...,
        min_length=5,
        max_length=5000,
        json_schema_extra={
            "example": (
                "Schedule a team sync with john@example.com and sarah@company.com"
                " tomorrow at 3 PM UTC for 45 minutes to discuss project roadmap."
            )
        },
    )


class FunctionCallResponse(BaseModel):
    id: int
    cached: bool
    function_call: ScheduleCalendarEventFunction


class AuthUser(BaseModel):
    id: int
    email: EmailStr
    name: Optional[str] = None
    picture: Optional[str] = None


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    result: Optional[dict] = None
    error: Optional[str] = None


class DLQReplayRequest(BaseModel):
    """Request model for replaying a DLQ message.

    `message_id` corresponds to the original Celery `task_id` stored in the DLQ.
    """

    message_id: str = Field(..., description="DLQ message/task id to replay")


class DLQDryRunResponse(BaseModel):
    message_id: str
    cached: bool = False
    parsed_function: Optional[dict] = None
    hmac_signature: Optional[str] = None
    note: Optional[str] = None


class PercentileMetrics(BaseModel):
    """Real-time percentile metrics for an operation."""

    operation: str = Field(..., description="Operation name")
    p50: float = Field(..., description="50th percentile latency (ms)")
    p95: float = Field(..., description="95th percentile latency (ms)")
    p99: float = Field(..., description="99th percentile latency (ms)")
    mean: float = Field(..., description="Mean latency (ms)")
    std_dev: float = Field(..., description="Standard deviation (ms)")
    sample_count: int = Field(..., description="Number of samples in window")
    anomaly_threshold: float = Field(
        ..., description="Anomaly detection threshold (ms)"
    )
    is_anomaly: bool = Field(..., description="Is current state anomalous")
    last_updated: str = Field(..., description="Last update timestamp")


class SystemHealthSnapshot(BaseModel):
    """Overall system health snapshot with all monitored operations."""

    timestamp: str = Field(..., description="Snapshot timestamp")
    total_anomalies: int = Field(..., description="Total anomalies detected")
    operations_monitored: list[str] = Field(
        ..., description="List of monitored operations"
    )
    metrics: dict[str, PercentileMetrics] = Field(
        ..., description="Metrics per operation"
    )
    circuit_breaker_state: dict[str, str] = Field(
        ..., description="Circuit breaker state per provider"
    )


class MetricsHistoryPoint(BaseModel):
    """Historical data point for chart visualization."""

    timestamp: str = Field(..., description="Data point timestamp")
    p50: float
    p95: float
    p99: float
    mean: float
    is_anomaly: bool
