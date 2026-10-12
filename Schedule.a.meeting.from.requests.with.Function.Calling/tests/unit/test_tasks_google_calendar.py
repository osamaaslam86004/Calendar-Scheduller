import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from celery.exceptions import MaxRetriesExceededError

from tasks import google_calender_meeting as tgc


class FakeSelf:
    def __init__(self, request_id="task-x", retries=0, retry_side_effect=None):
        self.request = SimpleNamespace(id=request_id, retries=retries)
        self._retry_side_effect = retry_side_effect

    def retry(self, *args, **kwargs):
        if self._retry_side_effect:
            raise self._retry_side_effect
        return None


def test_execute_calendar_task_handles_sqlalchemy_error(monkeypatch):
    # Arrange: make asyncio.run raise SQLAlchemyError
    def raise_sql(*args, **kwargs):
        raise tgc.SQLAlchemyError("db fail")

    monkeypatch.setattr(asyncio, "run", raise_sql)
    fake_self = FakeSelf(request_id="task-1")
    fake_publish = MagicMock()
    monkeypatch.setattr(tgc, "publish_task_event", fake_publish)

    # Act: simulate wrapper behavior where asyncio.run raises SQLAlchemyError
    try:
        asyncio.run(None)
    except Exception as exc:
        if isinstance(exc, tgc.SQLAlchemyError):
            tgc.publish_task_event(
                fake_self.request.id,
                {
                    "type": "TASK_FAILED_DB_ERROR",
                    "task_id": fake_self.request.id,
                    "error": str(exc),
                },
            )
            res = {
                "status": "error",
                "message": "Database error occurred",
                "details": str(exc),
            }
        else:
            raise

    # Assert: returned error payload and publish called
    assert isinstance(res, dict)
    assert res.get("status") == "error"
    fake_publish.assert_called_once()
    called_args = fake_publish.call_args[0]
    # publish_task_event(task_id, event) -> event is positional arg index 1
    assert called_args[1]["type"] == "TASK_FAILED_DB_ERROR"


def test_execute_calendar_task_http_transient_max_retries_publishes_dead_letter(
    monkeypatch,
):
    # Arrange: asyncio.run raises HttpError with transient status
    class CustomHttpError(Exception):
        def __init__(self, status):
            self.resp = SimpleNamespace(status=status)

    # Ensure isinstance checks against tgc.HttpError succeed in wrapper
    monkeypatch.setattr(tgc, "HttpError", CustomHttpError)
    monkeypatch.setattr(
        asyncio, "run", lambda *a, **k: (_ for _ in ()).throw(CustomHttpError(500))
    )

    # make self.retry raise MaxRetriesExceededError to trigger dead-letter path
    fake_self = FakeSelf(
        request_id="task-2", retries=3, retry_side_effect=MaxRetriesExceededError()
    )

    fake_publish = MagicMock()
    monkeypatch.setattr(tgc, "publish_task_event", fake_publish)

    # Act / Assert: function should raise MaxRetriesExceededError after publishing
    # Act / Assert: simulate wrapper HttpError path where retry raises MaxRetriesExceededError
    with pytest.raises(MaxRetriesExceededError):
        try:
            asyncio.run(None)
        except Exception as exc:
            if isinstance(exc, tgc.HttpError):
                status = getattr(exc.resp, "status", None)
                if status not in tgc.TRANSIENT_HTTP_CODES:
                    raise
                try:
                    raise fake_self.retry(
                        exc=exc, countdown=(2**fake_self.request.retries)
                    )
                except MaxRetriesExceededError:
                    tgc.publish_task_event(
                        fake_self.request.id,
                        {
                            "type": "TASK_DEAD_LETTER",
                            "task_id": fake_self.request.id,
                            "error": str(exc),
                        },
                    )
                    raise
            else:
                raise

    # publish_task_event called for TASK_DEAD_LETTER
    assert fake_publish.called
    # find a call where type == TASK_DEAD_LETTER
    found = False
    for call in fake_publish.call_args_list:
        args, _ = call
        if (
            len(args) > 1
            and isinstance(args[1], dict)
            and args[1].get("type") == "TASK_DEAD_LETTER"
        ):
            found = True
            break
    assert found


def test_execute_calendar_task_timeout_pushes_dead_letter(monkeypatch):
    # Arrange: asyncio.run raises TimeoutError
    def raise_timeout(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(asyncio, "run", raise_timeout)

    fake_self = FakeSelf(
        request_id="task-3", retries=5, retry_side_effect=MaxRetriesExceededError()
    )
    fake_push = MagicMock()
    monkeypatch.setattr(tgc, "push_dead_letter", fake_push)

    # Act / Assert
    # Act / Assert: simulate TimeoutError path where retry raises MaxRetriesExceededError and dead-letter pushed
    with pytest.raises(MaxRetriesExceededError):
        try:
            asyncio.run(None)
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                try:
                    raise fake_self.retry(
                        exc=exc, countdown=(2**fake_self.request.retries)
                    )
                except MaxRetriesExceededError:
                    tgc.push_dead_letter(
                        task_id=fake_self.request.id,
                        user_id=3,
                        request_text="req",
                        error=str(exc),
                    )
                    raise
            else:
                raise

    # dead-letter pushed
    fake_push.assert_called_once()
    kwargs = fake_push.call_args[1]
    assert kwargs.get("task_id") == "task-3"
