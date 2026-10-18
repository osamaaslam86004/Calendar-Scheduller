import logging

from middleware.tenant_context import TenantContextMiddleware

logger = logging.getLogger(__name__)


class TenantRBACMiddleware(TenantContextMiddleware):
    """Backward-compatible alias for the merged tenant validation + RBAC context middleware."""

    pass
