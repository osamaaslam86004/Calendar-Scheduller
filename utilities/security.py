# utilities/securiyt.py

# Example Generation
# import secrets
# print(secrets.token_hex(32)) # Generates 64 hex chars like: "a3f5b8..."

import base64
import contextvars
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import jwt
import redis
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import Depends, HTTPException, Request
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from config.settings import settings

logger = logging.getLogger(__name__)
MASTER_KEY = bytes.fromhex(settings.security.app_master_key)
JWT_ISSUER = "calendar-scheduling-api"
DEFAULT_TENANT_TOKEN_TTL_SECONDS = 3600
AUDIT_REDIS_KEY = "audit:events"
current_correlation_id = contextvars.ContextVar("current_correlation_id", default=None)


def generate_correlation_id() -> str:
    return uuid.uuid4().hex


def get_current_correlation_id() -> str | None:
    return current_correlation_id.get()


def set_current_correlation_id(correlation_id: str | None):
    if correlation_id is None:
        return current_correlation_id.set(None)
    return current_correlation_id.set(str(correlation_id))


def _canonical_json(value: dict) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def _tenant_secret() -> str:
    return settings.security.app_master_key


def build_tenant_context(
    tenant_id: int | str,
    scopes: list[str] | tuple[str, ...] | None = None,
    operation: str = "request",
    entity_key: str | None = None,
    bounds: dict | None = None,
    ttl_seconds: int | None = None,
) -> str:
    now = int(time.time())
    ttl = ttl_seconds or DEFAULT_TENANT_TOKEN_TTL_SECONDS
    payload = {
        "iss": JWT_ISSUER,
        "sub": str(tenant_id),
        "tenant_id": str(tenant_id),
        "user_id": tenant_id if isinstance(tenant_id, int) else str(tenant_id),
        "scopes": sorted(set(scopes or ["calendar:read", "calendar:write"])),
        "operation": operation,
        "entity_key": entity_key,
        "bounds": bounds or {},
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, _tenant_secret(), algorithm="HS256")


def verify_tenant_context(token: str | None) -> dict | None:
    if not token:
        return None

    try:
        payload = jwt.decode(
            token,
            _tenant_secret(),
            algorithms=["HS256"],
            issuer=JWT_ISSUER,
            options={"require": ["exp", "tenant_id", "scopes"]},
        )
    except Exception:
        logger.warning("Rejected invalid tenant JWT context", exc_info=True)
        return None

    if not payload.get("tenant_id"):
        return None

    return payload


def get_request_tenant_context(request: Request) -> dict | None:
    token = (
        request.session.get("tenant_context") if "session" in request.scope else None
    )

    if not token:
        token = request.headers.get("X-Tenant-Context") or request.headers.get(
            "X-Tenant-JWT"
        )

    if not token:
        auth = request.headers.get("Authorization")
        if auth and auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()

    return verify_tenant_context(token)


def validate_tenant_access(
    request: Request,
    required_scope: str,
    *,
    tenant_owner_id: int | str | None = None,
    resource_key: str | None = None,
) -> dict:
    claims = get_request_tenant_context(request)
    if not claims:
        raise HTTPException(status_code=401, detail="Tenant context required")

    scopes = set(claims.get("scopes", []))
    if required_scope not in scopes:
        raise HTTPException(status_code=403, detail=f"Missing scope: {required_scope}")

    if tenant_owner_id is not None:
        if str(claims.get("tenant_id")) != str(tenant_owner_id):
            raise HTTPException(
                status_code=403, detail="Tenant mismatch for requested resource"
            )

    if (
        resource_key is not None
        and claims.get("entity_key")
        and claims.get("entity_key") != resource_key
    ):
        raise HTTPException(
            status_code=403, detail="Entity key mismatch for tenant scope"
        )

    return claims


def append_audit_event(
    event_type: str,
    *,
    actor_id: int | str | None,
    tenant_id: int | str | None,
    action: str,
    resource: str,
    metadata: dict | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    """
    Purpose: Appends tamper-evident, HMAC-signed audit logs to a secure Redis queue.
    Destination: AUDIT_REDIS_KEY (a dedicated Redis key storing append-only audit logs
    for security, forensic traceability, compliance monitoring, and administrative analysis).
    """
    try:
        correlation_id = (
            correlation_id
            or request_id
            or get_current_correlation_id()
            or generate_correlation_id()
        )
        payload = {
            "event_type": event_type,
            "event_id": uuid.uuid4().hex,
            "actor_id": actor_id,
            "tenant_id": tenant_id,
            "action": action,
            "resource": resource,
            "metadata": metadata or {},
            "request_id": request_id or correlation_id,
            "correlation_id": correlation_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        digest = hmac.new(
            MASTER_KEY,
            _canonical_json(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        payload["audit_signature"] = digest

        redis_client = redis.Redis.from_url(settings.redis.url, decode_responses=True)
        redis_client.rpush(AUDIT_REDIS_KEY, json.dumps(payload))
        return payload
    except Exception:
        logger.exception("Failed to append tenant audit event")
        return {"event_type": event_type, "error": "audit_write_failed"}


def _get_kek(salt: bytes) -> AESGCM:
    """Derive a KEK from the Master Key using HKDF."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"envelope-encryption-kek",
    )
    return AESGCM(hkdf.derive(MASTER_KEY))


def encrypt_envelope(plaintext: str) -> str:
    """Encrypt data using a one-time DEK and wrap it in an envelope."""
    if plaintext is None:
        return None

    kek_salt = os.urandom(16)
    dek_raw = AESGCM.generate_key(bit_length=256)

    dek_cipher = AESGCM(dek_raw)
    data_nonce = os.urandom(12)
    ciphertext = dek_cipher.encrypt(data_nonce, plaintext.encode("utf-8"), None)

    kek_cipher = _get_kek(kek_salt)
    dek_nonce = os.urandom(12)
    encrypted_dek = kek_cipher.encrypt(dek_nonce, dek_raw, None)

    payload = {
        "kek_salt": base64.b64encode(kek_salt).decode("utf-8"),
        "dek_nonce": base64.b64encode(dek_nonce).decode("utf-8"),
        "encrypted_dek": base64.b64encode(encrypted_dek).decode("utf-8"),
        "data_nonce": base64.b64encode(data_nonce).decode("utf-8"),
        "ciphertext": base64.b64encode(ciphertext).decode("utf-8"),
    }
    return json.dumps(payload)


def decrypt_envelope(payload_json: str) -> str:
    """Decrypt a previously encrypted envelope payload."""
    if payload_json is None:
        return None

    payload = json.loads(payload_json)

    kek_salt = base64.b64decode(payload["kek_salt"])
    dek_nonce = base64.b64decode(payload["dek_nonce"])
    encrypted_dek = base64.b64decode(payload["encrypted_dek"])
    data_nonce = base64.b64decode(payload["data_nonce"])
    ciphertext = base64.b64decode(payload["ciphertext"])

    kek_cipher = _get_kek(kek_salt)
    dek_raw = kek_cipher.decrypt(dek_nonce, encrypted_dek, None)

    dek_cipher = AESGCM(dek_raw)
    plaintext_bytes = dek_cipher.decrypt(data_nonce, ciphertext, None)

    return plaintext_bytes.decode("utf-8")


class EncryptedString(TypeDecorator):
    """Transparently encrypts strings on save and decrypts on load."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            return encrypt_envelope(value)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            return decrypt_envelope(value)
        return value


def append_event_stream(
    stream_name: str,
    *,
    event_type: str,
    payload: dict,
    correlation_id: str | None = None,
    tenant_id: int | str | None = None,
) -> dict:
    """Append a structured event to a Redis Streams topic for pub/sub consumers."""
    try:
        event = {
            "event_type": event_type,
            "tenant_id": tenant_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "correlation_id": correlation_id
            or get_current_correlation_id()
            or generate_correlation_id(),
            "payload": json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
        }
        redis_client = redis.Redis.from_url(settings.redis.url, decode_responses=True)
        stream_id = redis_client.xadd(stream_name, event)
        return {"stream": stream_name, "id": stream_id, **event}
    except Exception:
        logger.exception("Failed to append event stream record for %s", stream_name)
        return {
            "stream": stream_name,
            "event_type": event_type,
            "error": "stream_write_failed",
        }


def record_latency_sample(
    *,
    operation: str,
    latency_ms: float,
    status: str = "ok",
    tenant_id: int | str | None = None,
    correlation_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Record latency samples for percentile-based provider monitoring."""
    event = {
        "operation": operation,
        "latency_ms": float(latency_ms),
        "status": status,
        "tenant_id": tenant_id,
        "metadata": metadata or {},
    }
    append_event_stream(
        "metrics:latency",
        event_type="latency_sample",
        payload=event,
        correlation_id=correlation_id,
        tenant_id=tenant_id,
    )
    return {
        "operation": operation,
        "latency_ms": float(latency_ms),
        "status": status,
        "correlation_id": correlation_id or get_current_correlation_id(),
    }


def ensure_stream_group(
    stream_name: str, group_name: str, *, start_id: str = "0"
) -> bool:
    """Ensure a Redis Streams consumer group exists for scalable workers."""
    try:
        redis_client = redis.Redis.from_url(settings.redis.url, decode_responses=True)
        try:
            redis_client.xgroup_create(
                stream_name, group_name, id=start_id, mkstream=True
            )
        except redis.exceptions.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        return True
    except Exception:
        logger.exception(
            "Failed to create Redis Streams consumer group %s for %s",
            group_name,
            stream_name,
        )
        return False


def read_stream_group(
    stream_name: str,
    group_name: str,
    consumer_name: str,
    *,
    block_ms: int = 5000,
    count: int = 10,
) -> list[dict]:
    """Read a batch of events for a consumer group without manual offset tracking."""
    try:
        redis_client = redis.Redis.from_url(settings.redis.url, decode_responses=True)
        messages = redis_client.xreadgroup(
            group_name, consumer_name, {stream_name: ">"}, count=count, block=block_ms
        )
        items = []
        for _, events in messages or []:
            for event_id, fields in events:
                items.append({"id": event_id, "fields": fields})
        return items
    except Exception:
        logger.exception(
            "Failed to read Redis Streams group %s/%s", stream_name, group_name
        )
        return []


# --- TENANT CONTEXT & AUDIT LOGGING DEPENDENCIES ---

def get_current_tenant_context(request: Request) -> dict:
    """FastAPI dependency to securely retrieve validated tenant context."""
    context = getattr(request.state, "tenant_context", None)
    if not context:
        raise HTTPException(
            status_code=401, 
            detail="Tenant context missing. TenantRBACMiddleware must be installed."
        )
    return context


class AuditLogger:
    """
    A tenant-scoped audit logger dependency.
    Automatically injects the active tenant ID and fallback mode into the audit record.
    """
    def __init__(
        self, 
        request: Request, 
        tenant_context: dict = Depends(get_current_tenant_context)
    ):
        self.request = request
        self.tenant_context = tenant_context
        self.tenant_id = tenant_context.get("tenant_id")
        self.fallback_mode = tenant_context.get("fallback_mode", False)
        # Attempt to pull user_id if authenticated
        self.user_id = getattr(request.state, "user_id", None)

    def log(self, event_type: str, action: str, resource: str, metadata: dict | None = None) -> dict:
        """Trigger an audit log entry explicitly bound to the current tenant."""
        meta = metadata or {}
        meta["fallback_mode"] = self.fallback_mode
        
        return append_audit_event(
            event_type=event_type,
            actor_id=self.user_id,
            tenant_id=self.tenant_id,
            action=action,
            resource=resource,
            metadata=meta
        )


def get_audit_logger(logger: AuditLogger = Depends(AuditLogger)) -> AuditLogger:
    """FastAPI dependency injection for the scoped AuditLogger."""
    return logger
