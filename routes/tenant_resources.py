from fastapi import APIRouter, Depends
from utilities.security import get_current_tenant_context, AuditLogger, get_audit_logger

router = APIRouter(prefix="/api/v1/tenant-resources", tags=["tenant-resources"])


@router.get("/")
def get_tenant_info(tenant_context: dict = Depends(get_current_tenant_context)):
    """Retrieve current tenant info via injected context dependency."""
    return {
        "message": "Tenant access granted",
        "tenant_id": tenant_context.get("tenant_id"),
        "fallback_mode": tenant_context.get("fallback_mode"),
    }


@router.post("/action")
def perform_tenant_action(
    payload: dict, logger: AuditLogger = Depends(get_audit_logger)
):
    """
    Perform a tenant-scoped action.
    The AuditLogger dependency automatically captures the tenant_id and fallback_mode
    from the request context and binds it to the audit event.
    """
    # Perform some mock action here...

    # Automatically emit an audit log bound to this tenant
    audit_record = logger.log(
        event_type="tenant_resource_mutated",
        action="update_resource",
        resource="mock_resource_id",
        metadata={"payload_keys": list(payload.keys())},
    )

    return {"status": "success", "audit_emitted": audit_record}
