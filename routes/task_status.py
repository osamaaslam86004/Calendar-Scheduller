# Keep the endpoint for compatibility
# But the primary integration should now use WebSocket events,

# routes/task_status.py

from celery.result import AsyncResult
from fastapi import APIRouter, HTTPException, Path, status, Request

from config.limiter import limiter
from schemas import TaskStatusResponse
from celery_app import celery_app

router = APIRouter()

# Matches standard UUID4 format (36 chars)
UUID4_REGEX = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@router.get(
    "/api/v1/tasks/{task_id}",
    response_model=TaskStatusResponse,
)
@limiter.limit("60/minute")  # Polling-friendly rate limit
def get_task_status(
    request: Request,
    task_id: str = Path(
        ...,
        min_length=36,
        max_length=36,
        pattern=UUID4_REGEX,
        description="UUID v4 standard Celery task identifier",
    ),
):
    # Retrieve the exact key used by Celery in Redis (e.g., 'celery-task-meta-<task_id>')
    task_key = celery_app.backend.get_key_for_task(task_id)

    # Check if task metadata exists in Redis backend
    if not celery_app.backend.get(task_key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task ID '{task_id}' was not found.",
        )

    result = AsyncResult(task_id, app=celery_app)

    if result.state == "PENDING":

        return TaskStatusResponse(
            task_id=task_id,
            status="PENDING",
        )

    if result.state == "STARTED":

        return TaskStatusResponse(
            task_id=task_id,
            status="STARTED",
        )

    if result.state == "SUCCESS":

        return TaskStatusResponse(
            task_id=task_id,
            status="SUCCESS",
            result=result.result,
        )

    if result.failed():

        return TaskStatusResponse(
            task_id=task_id,
            status="FAILURE",
            error=str(result.result),
        )

    return TaskStatusResponse(
        task_id=task_id,
        status=result.state,
    )
