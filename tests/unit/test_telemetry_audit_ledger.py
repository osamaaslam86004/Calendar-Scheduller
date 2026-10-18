"""Unit tests for telemetry/audit_ledger.py.

Covers:
- PII masking via regex patterns (email, phone, SSN, credit cards, composite text).
- Numeric digit masking helper (_mask_digits).
- AuditLedger initialization, directory creation, and state recovery from disk.
- Monotonic indexing and in-memory state preservation.
- HMAC-SHA256 block signature generation and cryptographic chaining.
- Cryptographic chain integrity verification (verify_chain), including:
  - Valid empty and multi-block chains
  - Detection of tampered data
  - Detection of tampered block hashes
  - Detection of corrupted JSON entries
  - Verification with mismatched HMAC keys
- Background asynchronous writer loop, batch draining, disk syncing, and lifecycle (start/stop).
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telemetry.audit_ledger import AuditLedger, _mask_digits

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def hmac_key() -> bytes:
    """Fixed 32-byte secret key for deterministic testing."""
    return b"test_secret_key_32_bytes_long!!!"


@pytest.fixture
def ledger_path(tmp_path) -> str:
    """Path to a temporary ledger file."""
    return str(tmp_path / "audit_test.log")


# ============================================================================
# PII Masking Tests
# ============================================================================


@pytest.mark.unit
class TestAuditLedgerPIIMasking:
    """Tests for PII identification and masking middleware."""

    def test_mask_digits_helper(self):
        """Test _mask_digits with various lengths and boundaries."""
        # Short digits <= keep_last (default 2)
        assert _mask_digits("5") == "[MASKED]"
        assert _mask_digits("42") == "[MASKED]"

        # Standard digits
        assert _mask_digits("12345", keep_last=2) == "***45"
        assert _mask_digits("+1-555-1234", keep_last=2) == "******34"
        assert _mask_digits("non-digits-only!!!") == "[MASKED]"

    def test_mask_email_addresses(self):
        """Test email masking retains domain while replacing local-part."""
        text = "Contact user alice@example.com or bob.smith+tag@sub.domain.org."
        masked = AuditLedger.mask_pii(text)
        assert "[MASKED]@example.com" in masked
        assert "[MASKED]@sub.domain.org" in masked
        assert "alice" not in masked
        assert "bob.smith" not in masked

    def test_mask_phone_numbers(self):
        """Test phone number masking keeps only trailing 2 digits."""
        text = "Direct calls to +1 (555) 234-5678 or 555-876-5432."
        masked = AuditLedger.mask_pii(text)
        assert "234-5678" not in masked
        assert "876-5432" not in masked
        assert masked.endswith("32.") or "32" in masked

    def test_mask_ssn(self):
        """Test SSN masking replaces standard xxx-xx-xxxx format."""
        text = "Employee SSN is 123-45-6789 on record."
        masked = AuditLedger.mask_pii(text)
        assert "123-45-6789" not in masked
        assert "[MASKED_SSN]" in masked

    def test_mask_credit_cards(self):
        """Test credit card masking for spaced and contiguous 13-16 digit strings."""
        text = "Card 4111 2222 3333 4444 and alternate 4532015678901234 charged."
        masked = AuditLedger.mask_pii(text)
        assert "4111 2222 3333 4444" not in masked
        assert "4532015678901234" not in masked
        assert masked.count("[MASKED_CC]") == 2

    def test_mask_composite_json_string(self):
        """Test composite JSON data with multiple PII fields masked in one pass."""
        payload = {
            "user": "alice@corp.com",
            "phone": "+1-800-555-0199",
            "ssn": "987-65-4321",
            "cc": "4000123456789010",
            "action": "login_attempt",
            "status": "success",
        }
        json_str = json.dumps(payload)
        masked = AuditLedger.mask_pii(json_str)

        assert "alice" not in masked
        assert "[MASKED]@corp.com" in masked
        assert "987-65-4321" not in masked
        assert "[MASKED_SSN]" in masked
        assert "4000123456789010" not in masked
        assert "[MASKED_CC]" in masked
        # Safe non-PII attributes must remain untouched
        assert "login_attempt" in masked
        assert "success" in masked

    def test_mask_benign_text_unchanged(self):
        """Test benign text containing no PII is preserved exactly."""
        benign = "Server health check passed with status OK at timestamp 2026-10-13."
        assert AuditLedger.mask_pii(benign) == benign


# ============================================================================
# Initialization and State Recovery Tests
# ============================================================================


@pytest.mark.unit
class TestAuditLedgerInitializationAndState:
    """Tests for ledger disk persistence, directory creation, and recovery."""

    def test_init_creates_parent_directories(self, tmp_path, hmac_key):
        """Test initializing ledger in deeply nested non-existent directory creates parents."""
        nested_file = tmp_path / "deeply" / "nested" / "logs" / "audit.log"
        assert not nested_file.parent.exists()

        ledger = AuditLedger(file_path=str(nested_file), hmac_key=hmac_key)
        assert nested_file.parent.exists()
        assert ledger._last_index == -1
        assert ledger._last_hash == ""

    def test_init_fresh_state_when_file_not_found(self, ledger_path, hmac_key):
        """Test new ledger instance initializes with index -1 and empty hash."""
        ledger = AuditLedger(file_path=ledger_path, hmac_key=hmac_key)
        state = ledger.latest()
        assert state == {"index": -1, "block_hash": ""}

    def test_load_state_from_existing_valid_file(self, ledger_path, hmac_key):
        """Test ledger re-initialization restores index and hash from last block on disk."""
        blocks = [
            {"index": 0, "block_hash": "hash_0", "operation": "op0", "data": "d0"},
            {"index": 1, "block_hash": "hash_1", "operation": "op1", "data": "d1"},
            {"index": 2, "block_hash": "hash_2", "operation": "op2", "data": "d2"},
        ]
        with open(ledger_path, "w", encoding="utf-8") as f:
            for b in blocks:
                f.write(json.dumps(b) + "\n")

        ledger = AuditLedger(file_path=ledger_path, hmac_key=hmac_key)
        assert ledger._last_index == 2
        assert ledger._last_hash == "hash_2"
        assert ledger.latest() == {"index": 2, "block_hash": "hash_2"}

    def test_load_state_empty_or_whitespace_file(self, ledger_path, hmac_key):
        """Test state loading on empty or whitespace-only file defaults cleanly."""
        with open(ledger_path, "w", encoding="utf-8") as f:
            f.write("\n   \n\n")

        ledger = AuditLedger(file_path=ledger_path, hmac_key=hmac_key)
        assert ledger._last_index == -1
        assert ledger._last_hash == ""

    def test_load_state_corrupt_tail_line_fallback(self, ledger_path, hmac_key):
        """Test state loading skips malformed trailing lines to find last valid entry."""
        with open(ledger_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"index": 5, "block_hash": "hash_valid"}) + "\n")
            f.write("corrupt_non_json_line\n")

        ledger = AuditLedger(file_path=ledger_path, hmac_key=hmac_key)
        assert ledger._last_index == 5
        assert ledger._last_hash == "hash_valid"

    def test_load_state_handles_os_error_gracefully(self, ledger_path, hmac_key):
        """Test _load_state exception logs error and continues with clean state."""
        with open(ledger_path, "w", encoding="utf-8") as f:
            f.write("test content\n")

        with patch.object(
            AuditLedger, "_load_state", side_effect=OSError("Read error")
        ):
            ledger = AuditLedger(file_path=ledger_path, hmac_key=hmac_key)
            assert ledger._last_index == -1


# ============================================================================
# HMAC Generation & Chain Verification Tests
# ============================================================================


@pytest.mark.unit
class TestAuditLedgerHMACAndChainIntegrity:
    """Tests for HMAC generation and cryptographic chain integrity verification."""

    def test_compute_block_hmac_deterministic(self, ledger_path, hmac_key):
        """Test _compute_block_hmac generates deterministic hashes for identical inputs."""
        ledger = AuditLedger(file_path=ledger_path, hmac_key=hmac_key)
        mac1 = ledger._compute_block_hmac(
            index=0,
            timestamp="2026-10-13T00:00:00Z",
            operation="auth_login",
            data="{}",
            trace_id="tr-123",
            prev_hash="",
        )
        mac2 = ledger._compute_block_hmac(
            index=0,
            timestamp="2026-10-13T00:00:00Z",
            operation="auth_login",
            data="{}",
            trace_id="tr-123",
            prev_hash="",
        )
        assert mac1 == mac2
        assert len(mac1) == 64  # SHA-256 hex length

        # Different input must produce different hash
        mac3 = ledger._compute_block_hmac(
            index=1,
            timestamp="2026-10-13T00:00:00Z",
            operation="auth_login",
            data="{}",
            trace_id="tr-123",
            prev_hash=mac1,
        )
        assert mac3 != mac1

    def test_verify_chain_nonexistent_file_returns_valid(self, tmp_path, hmac_key):
        """Test verify_chain returns (True, []) when file does not exist."""
        nonexistent = str(tmp_path / "does_not_exist.log")
        ledger = AuditLedger(file_path=nonexistent, hmac_key=hmac_key)
        valid, invalid_indexes = ledger.verify_chain()
        assert valid is True
        assert invalid_indexes == []

    def test_append_and_verify_valid_chain(self, ledger_path, hmac_key):
        """Test appending blocks synchronously into batch writes valid chain."""
        ledger = AuditLedger(file_path=ledger_path, hmac_key=hmac_key)

        blocks = []
        for i in range(5):
            ledger.append(
                operation=f"op_{i}",
                data={"item": i, "email": f"user{i}@test.com"},
                trace_id=f"trace-{i}",
                timestamp=f"2026-10-13T01:0{i}:00Z",
            )
            blocks.append(ledger._queue.get_nowait())

        # Write directly to disk
        ledger._write_batch(blocks)

        valid, invalid = ledger.verify_chain()
        assert valid is True
        assert invalid == []

    def test_verify_chain_detects_tampered_data(self, ledger_path, hmac_key):
        """Test verify_chain detects content modification in an audit block."""
        ledger = AuditLedger(file_path=ledger_path, hmac_key=hmac_key)

        blocks = []
        for i in range(4):
            ledger.append(
                operation=f"op_{i}",
                data={"index": i},
                trace_id=f"trace-{i}",
                timestamp=f"2026-10-13T01:0{i}:00Z",
            )
            blocks.append(ledger._queue.get_nowait())

        ledger._write_batch(blocks)

        # Tamper with block index 1's data on disk
        with open(ledger_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]

        lines[1]["data"] = '{"tampered": true}'

        with open(ledger_path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

        valid, invalid = ledger.verify_chain()
        assert valid is False
        # Block 1 is invalid, and subsequent block 2 fails prev_hash verification
        assert 1 in invalid

    def test_verify_chain_detects_tampered_block_hash(self, ledger_path, hmac_key):
        """Test verify_chain detects forged block_hash."""
        ledger = AuditLedger(file_path=ledger_path, hmac_key=hmac_key)

        blocks = []
        for i in range(3):
            ledger.append(
                operation=f"op_{i}", data="clean", timestamp=f"2026-10-13T00:0{i}:00Z"
            )
            blocks.append(ledger._queue.get_nowait())

        ledger._write_batch(blocks)

        with open(ledger_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]

        lines[0][
            "block_hash"
        ] = "0000000000000000000000000000000000000000000000000000000000000000"

        with open(ledger_path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

        valid, invalid = ledger.verify_chain()
        assert valid is False
        assert 0 in invalid

    def test_verify_chain_detects_corrupt_json_lines(self, ledger_path, hmac_key):
        """Test verify_chain records unparseable lines as invalid."""
        ledger = AuditLedger(file_path=ledger_path, hmac_key=hmac_key)
        ledger.append(operation="init", data="ok", timestamp="2026-10-13T00:00:00Z")
        block = ledger._queue.get_nowait()
        ledger._write_batch([block])

        # Append corrupted raw string
        with open(ledger_path, "a", encoding="utf-8") as f:
            f.write("{invalid json syntax\n")

        valid, invalid = ledger.verify_chain()
        assert valid is False
        assert len(invalid) > 0

    def test_verify_chain_with_mismatched_key_fails(self, ledger_path, hmac_key):
        """Test verifying ledger with an incorrect HMAC secret key marks blocks invalid."""
        ledger = AuditLedger(file_path=ledger_path, hmac_key=hmac_key)
        ledger.append(operation="op", data="data", timestamp="2026-10-13T00:00:00Z")
        ledger._write_batch([ledger._queue.get_nowait()])

        # Second ledger with different key
        other_ledger = AuditLedger(
            file_path=ledger_path, hmac_key=b"completely_different_key_32_bytes"
        )
        valid, invalid = other_ledger.verify_chain()
        assert valid is False
        assert 0 in invalid


# ============================================================================
# Async Writer Loop and Lifecycle Tests
# ============================================================================


@pytest.mark.unit
class TestAuditLedgerAsyncWriterAndLifecycle:
    """Tests for background writer queue, batching, disk writes, and stop semantics."""

    @pytest.mark.anyio
    async def test_monotonic_indexing_under_concurrent_appends(
        self, ledger_path, hmac_key
    ):
        """Test successive appends maintain monotonic index increment in memory immediately."""
        ledger = AuditLedger(file_path=ledger_path, hmac_key=hmac_key)

        for i in range(10):
            ledger.append(operation=f"op_{i}", data={"val": i})
            assert ledger._last_index == i

        assert ledger._queue.qsize() == 10

    @pytest.mark.anyio
    async def test_writer_loop_lifecycle_and_flush(self, ledger_path, hmac_key):
        """Test background writer starts, writes queued items, and flushes on stop."""
        ledger = AuditLedger(
            file_path=ledger_path, hmac_key=hmac_key, flush_interval=0.05
        )
        ledger.start()

        # Enqueue items
        for i in range(5):
            ledger.append(
                operation=f"async_op_{i}",
                data={"email": f"user{i}@test.com"},
                trace_id=f"tr-{i}",
            )

        # Stop should cleanly flush all items to disk
        await ledger.stop()

        assert os.path.exists(ledger_path)
        with open(ledger_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        assert len(lines) == 5
        for i, line in enumerate(lines):
            assert line["index"] == i
            assert line["operation"] == f"async_op_{i}"
            assert "[MASKED]@test.com" in line["data"]

        # Ledger state matches
        assert ledger._last_index == 4

        # Chain verification succeeds
        valid, invalid = ledger.verify_chain()
        assert valid is True
        assert invalid == []

    @pytest.mark.anyio
    async def test_start_is_idempotent(self, ledger_path, hmac_key):
        """Test calling start multiple times does not spawn duplicate tasks."""
        ledger = AuditLedger(file_path=ledger_path, hmac_key=hmac_key)
        ledger.start()
        first_task = ledger._writer_task

        ledger.start()
        assert ledger._writer_task is first_task

        await ledger.stop()

    @pytest.mark.anyio
    async def test_stop_when_not_started(self, ledger_path, hmac_key):
        """Test calling stop on an unstarted ledger completes without error."""
        ledger = AuditLedger(file_path=ledger_path, hmac_key=hmac_key)
        assert ledger._writer_task is None
        await ledger.stop()

    def test_write_batch_fsync_error_tolerated(self, ledger_path, hmac_key):
        """Test os.fsync or os.open failure inside _write_batch is non-fatal."""
        ledger = AuditLedger(file_path=ledger_path, hmac_key=hmac_key)
        block = {
            "index": 0,
            "timestamp": "2026-10-13T00:00:00Z",
            "operation": "test",
            "data": "{}",
            "trace_id": None,
            "prev_hash": "",
            "block_hash": "hash",
        }

        with patch("os.open", side_effect=OSError("Permission denied on fsync")):
            # Must write file without raising
            ledger._write_batch([block])

        assert os.path.exists(ledger_path)
        with open(ledger_path, "r", encoding="utf-8") as f:
            content = f.read()
            assert "test" in content

    def test_append_with_non_serializable_data(self, ledger_path, hmac_key):
        """Test append falls back to str(data) when object is not JSON-serializable."""
        ledger = AuditLedger(file_path=ledger_path, hmac_key=hmac_key)

        class NonSerializable:
            def __str__(self):
                return "NonSerializableObject(id=123, email=admin@root.com)"

        ledger.append(operation="custom_object", data=NonSerializable())
        queued_item = ledger._queue.get_nowait()
        assert "NonSerializableObject" in queued_item["data"]
        assert "[MASKED]@root.com" in queued_item["data"]
