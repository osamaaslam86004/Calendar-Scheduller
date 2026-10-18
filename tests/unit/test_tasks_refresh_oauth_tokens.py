import pytest
from unittest.mock import MagicMock

from tasks import refresh_oauth_tokens as rot


def test_rotate_oauth_tokens_success(monkeypatch):
    # Arrange: asyncio.run returns number of refreshed tokens
    monkeypatch.setattr("asyncio.run", lambda *args, **kwargs: 3)
    fake_append = MagicMock()
    monkeypatch.setattr(rot, "append_audit_event", fake_append)

    # Act
    res = rot.rotate_oauth_tokens()

    # Assert
    assert res == {"refreshed": 3}
    assert fake_append.called
    assert fake_append.call_args[0][0] == "oauth_rotation_completed"


def test_rotate_oauth_tokens_failure_appends_and_raises(monkeypatch):
    # Arrange: asyncio.run raises
    def raise_exc(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("asyncio.run", raise_exc)
    fake_append = MagicMock()
    monkeypatch.setattr(rot, "append_audit_event", fake_append)

    # Act / Assert
    with pytest.raises(RuntimeError):
        rot.rotate_oauth_tokens()

    assert fake_append.called
    assert fake_append.call_args[0][0] == "oauth_rotation_failed"
