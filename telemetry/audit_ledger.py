import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


class AuditLedger:
    """Append-only immutable audit ledger.

    Features:
    - Each block contains an HMAC-SHA256 over its fields chained to previous block's hash.
    - PII masking applied to stored data via regex middleware.
    - Async queue with a background writer to batch appends to disk.

    Storage format: newline-delimited JSON blocks.
    """

    PII_PATTERNS: List[Tuple[re.Pattern, str]] = [
        # email: mask local-part
        (
            re.compile(r"([a-zA-Z0-9_.+-]+)@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)"),
            lambda m: "[MASKED]@" + m.group(2),
        ),
        # SSN-like (xxx-xx-xxxx)
        (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[MASKED_SSN]"),
        # Credit card-like numbers (13-16 digits)
        (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "[MASKED_CC]"),
        # US-like phone numbers (standard 7-digit or 10/11-digit with optional country/area code): mask digits leaving last 2
        (
            re.compile(
                r"(\b(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}\b|\b\d{3}[-.\s]\d{4}\b)"
            ),
            lambda m: _mask_digits(m.group(1), 2),
        ),
    ]

    def __init__(
        self,
        file_path: str,
        hmac_key: bytes,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        flush_interval: float = 0.5,
    ) -> None:
        self.file_path = Path(file_path)
        self.hmac_key = hmac_key
        self.flush_interval = flush_interval
        try:
            self.loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            try:
                self.loop = loop or asyncio.get_event_loop()
            except RuntimeError:
                self.loop = None

        self._queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        self._writer_task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

        # state
        self._last_index = -1
        self._last_hash = ""

        # ensure dir exists
        if not self.file_path.parent.exists():
            self.file_path.parent.mkdir(parents=True, exist_ok=True)

        # load existing ledger state (if any)
        try:
            self._load_state()
        except Exception:
            logger.exception("Failed to load ledger state; starting fresh")

    def start(self) -> None:
        """Start background writer task. Call once when app starts."""
        if self.loop is None:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError:
                try:
                    self.loop = asyncio.get_event_loop()
                except RuntimeError:
                    return
        if self.loop and (self._writer_task is None or self._writer_task.done()):
            self._stopping.clear()
            self._writer_task = self.loop.create_task(self._writer_loop())
            logger.debug("AuditLedger writer started")

    async def stop(self) -> None:
        """Stop background writer and flush remaining entries."""
        self._stopping.set()
        if self._writer_task:
            await self._writer_task
            logger.debug("AuditLedger writer stopped")

    def _load_state(self) -> None:
        """Read last line to set index and last hash."""
        if not self.file_path.exists():
            return
        # read last non-empty line
        with self.file_path.open("rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 4096), os.SEEK_SET)
            except OSError:
                # file small
                f.seek(0)
            lines = f.read().splitlines()
        if not lines:
            return
        # find last non-empty
        for raw in reversed(lines):
            if raw.strip():
                try:
                    last = json.loads(raw.decode("utf-8"))
                    self._last_index = int(last.get("index", -1))
                    self._last_hash = last.get("block_hash", "")
                    return
                except Exception:
                    continue

    @classmethod
    def mask_pii(cls, text: str) -> str:
        """Apply PII masking patterns to a text string."""
        out = text
        for patt, repl in cls.PII_PATTERNS:
            if callable(repl):
                out = patt.sub(repl, out)
            else:
                out = patt.sub(repl, out)
        return out

    def _compute_block_hmac(
        self,
        index: int,
        timestamp: str,
        operation: str,
        data: str,
        trace_id: Optional[str],
        prev_hash: str,
    ) -> str:
        payload = "|".join(
            [str(index), timestamp, operation, data, str(trace_id or ""), prev_hash]
        )
        mac = hmac.new(
            self.hmac_key, payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return mac

    def append(
        self,
        operation: str,
        data: Any,
        trace_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> None:
        """Enqueue an audit block for append.

        `data` may be a dict/str/any JSON-serializable object; PII masking will be applied to its JSON string.
        """
        ts = timestamp or datetime.datetime.utcnow().isoformat() + "Z"

        # prepare data string
        try:
            data_text = json.dumps(data, default=str, ensure_ascii=False)
        except Exception:
            data_text = str(data)

        masked = self.mask_pii(data_text)

        index = self._last_index + 1
        prev_hash = self._last_hash or ""
        block_hash = self._compute_block_hmac(
            index, ts, operation, masked, trace_id, prev_hash
        )

        block = {
            "index": index,
            "timestamp": ts,
            "operation": operation,
            "data": masked,
            "trace_id": trace_id,
            "prev_hash": prev_hash,
            "block_hash": block_hash,
        }

        # update in-memory state immediately to preserve monotonic indexes for concurrent callers
        self._last_index = index
        self._last_hash = block_hash

        # enqueue for background write
        self._queue.put_nowait(block)

    async def _writer_loop(self) -> None:
        """Background writer that flushes queued blocks to disk in batches."""
        batch: List[Dict[str, Any]] = []
        flush_interval = self.flush_interval
        while not (self._stopping.is_set() and self._queue.empty()):
            try:
                # gather up to a small batch
                try:
                    item = await asyncio.wait_for(
                        self._queue.get(), timeout=flush_interval
                    )
                    batch.append(item)
                    # drain quickly
                    while not self._queue.empty() and len(batch) < 100:
                        batch.append(self._queue.get_nowait())
                except asyncio.TimeoutError:
                    pass

                if batch:
                    # write batch to disk synchronously in thread
                    await asyncio.to_thread(self._write_batch, batch)
                    batch.clear()
            except Exception:
                logger.exception("Error in audit ledger writer loop")

        # final flush if any
        if batch:
            await asyncio.to_thread(self._write_batch, batch)

    def _write_batch(self, batch: Iterable[Dict[str, Any]]) -> None:
        # open file and append each block as a JSON line
        with self.file_path.open("a", encoding="utf-8") as f:
            for block in batch:
                f.write(json.dumps(block, ensure_ascii=False) + "\n")
        # ensure data durable
        try:
            fd = os.open(self.file_path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except Exception:
            # fsync failure is non-fatal here
            pass

    def verify_chain(self) -> Tuple[bool, List[int]]:
        """Verify the integrity of the ledger file using the HMAC key.

        Returns (is_valid, list_of_invalid_indexes)
        """
        if not self.file_path.exists():
            return True, []

        invalid: List[int] = []
        prev_hash = ""
        idx = -1
        with self.file_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    blk = json.loads(line)
                except Exception:
                    invalid.append(idx + 1)
                    continue
                idx = int(blk.get("index", -1))
                timestamp = blk.get("timestamp", "")
                operation = blk.get("operation", "")
                data = blk.get("data", "")
                trace_id = blk.get("trace_id", None)
                stored_hash = blk.get("block_hash", "")
                # recompute
                computed = self._compute_block_hmac(
                    idx, timestamp, operation, data, trace_id, prev_hash
                )
                if not hmac.compare_digest(computed, stored_hash):
                    invalid.append(idx)
                prev_hash = stored_hash

        return (len(invalid) == 0, invalid)

    def latest(self) -> Dict[str, Any]:
        """Return in-memory last index and hash."""
        return {"index": self._last_index, "block_hash": self._last_hash}


def _mask_digits(s: str, keep_last: int = 2) -> str:
    digits = re.sub(r"\D", "", s)
    if len(digits) <= keep_last:
        return "[MASKED]"
    masked = "*" * (len(digits) - keep_last) + digits[-keep_last:]
    return masked


__all__ = ["AuditLedger"]
