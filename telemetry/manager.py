import asyncio
import os
import logging
from typing import Optional

from telemetry.audit_ledger import AuditLedger
from config.settings import settings

_shared_ledger: Optional[AuditLedger] = None


def _resolve_hmac_key() -> bytes:
    try:
        return bytes.fromhex(settings.security.app_master_key)
    except Exception:
        return os.environ.get("AUDIT_LEDGER_HMAC_KEY", "").encode("utf-8")


def _resolve_path() -> str:
    return os.environ.get("AUDIT_LEDGER_PATH", "telemetry/audit.log")


def get_shared_ledger() -> AuditLedger:
    """Return a singleton AuditLedger, starting its writer if not started."""
    global _shared_ledger
    if _shared_ledger is None:
        hmac_key = _resolve_hmac_key()
        path = _resolve_path()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:

            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None

        ledger = AuditLedger(file_path=path, hmac_key=hmac_key, loop=loop)

        # start ledger writer; ledger.start handles None loop
        ledger.start()
        _shared_ledger = ledger

        logging.getLogger(__name__).debug("Initialized shared AuditLedger at %s", path)

    return _shared_ledger


def stop_shared_ledger() -> None:
    """Stop shared ledger writer synchronously if running."""
    global _shared_ledger

    if _shared_ledger is not None:
        try:
            # try to stop using asyncio if possible
            loop = asyncio.get_event_loop()

            if loop.is_running():
                # schedule stop
                loop.create_task(_shared_ledger.stop())
            else:
                loop.run_until_complete(_shared_ledger.stop())

        except Exception:
            try:
                # fallback to synchronous stop call
                asyncio.run(_shared_ledger.stop())
            except Exception:

                logging.getLogger(__name__).exception("Failed to stop shared ledger")

        _shared_ledger = None
