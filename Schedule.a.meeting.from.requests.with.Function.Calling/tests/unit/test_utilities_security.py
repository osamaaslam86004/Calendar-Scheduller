import json
import pytest
from unittest.mock import patch, MagicMock

pytest.importorskip("utilities.security")
from utilities.security import (
    generate_correlation_id,
    get_current_correlation_id,
    set_current_correlation_id,
    build_tenant_context,
    verify_tenant_context,
    append_audit_event,
    encrypt_envelope,
    decrypt_envelope,
)


@pytest.mark.unit
def test_correlation_id_helpers_round_trip():
    cid = generate_correlation_id()
    assert isinstance(cid, str)
    assert len(cid) > 0

    set_current_correlation_id(cid)
    assert get_current_correlation_id() == cid

    set_current_correlation_id(None)
    assert get_current_correlation_id() is None


@pytest.mark.unit
def test_build_and_verify_tenant_context():
    token = build_tenant_context(
        "tenant-42", scopes=["calendar:read"], operation="demo", ttl_seconds=3600
    )
    assert isinstance(token, str)
    payload = verify_tenant_context(token)
    assert payload is not None
    assert payload["tenant_id"] == "tenant-42"


@pytest.mark.unit
def test_append_audit_event_uses_redis_and_returns_payload():
    with patch("utilities.security.redis.Redis.from_url") as fake_factory:
        fake_client = MagicMock()
        fake_factory.return_value = fake_client
        result = append_audit_event(
            "event-type",
            actor_id="u1",
            tenant_id="t1",
            action="action",
            resource="resource",
            metadata={"ok": True},
            request_id="r1",
            correlation_id="c1",
        )
        assert result["event_type"] == "event-type"
        fake_client.rpush.assert_called_once()


@pytest.mark.unit
def test_envelope_encrypt_and_decrypt_round_trip():
    wrapped = encrypt_envelope("super secret")
    assert isinstance(wrapped, str)
    assert decrypt_envelope(wrapped) == "super secret"
