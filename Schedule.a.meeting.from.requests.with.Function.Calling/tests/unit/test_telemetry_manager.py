"""Unit tests for telemetry/manager.py: Singleton AuditLedger Manager and Lifecycle.

Covers:
- HMAC key resolution:
  - Settings master key hex decoding
  - Fallback to AUDIT_LEDGER_HMAC_KEY environment variable
  - Default empty bytes fallback
- Path resolution:
  - Default "telemetry/audit.log"
  - AUDIT_LEDGER_PATH environment variable override
- Singleton ledger management (get_shared_ledger):
  - Initialization of AuditLedger instance
  - Automatic writer startup
  - Singleton identity preservation across multiple calls
  - Handling of missing/runtime error event loop states
- Lifecycle shutdown (stop_shared_ledger):
  - No-op when _shared_ledger is None
  - Shutdown via loop.create_task when event loop is running
  - Shutdown via loop.run_until_complete when event loop is stopped
  - Fallback to asyncio.run when loop access fails
  - Exception resilience and global singleton clearing
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import telemetry.manager as manager
from telemetry.audit_ledger import AuditLedger


@pytest.fixture(autouse=True)
def reset_shared_ledger():
    """Ensure manager._shared_ledger is reset before and after every test."""
    manager._shared_ledger = None
    yield
    manager._shared_ledger = None


# ============================================================================
# Resolvers Tests
# ============================================================================


@pytest.mark.unit
class TestTelemetryManagerResolvers:
    """Tests for HMAC key and path resolution from config and environment."""

    def test_resolve_hmac_key_from_settings(self):
        """Test resolving HMAC key from settings.security.app_master_key hex string."""
        sample_hex = "a" * 64  # 32 bytes of 0xaa
        with patch.object(manager.settings.security, "app_master_key", sample_hex):
            key = manager._resolve_hmac_key()
            assert key == bytes.fromhex(sample_hex)
            assert len(key) == 32

    def test_resolve_hmac_key_fallback_to_env_var(self, monkeypatch):
        """Test fallback to AUDIT_LEDGER_HMAC_KEY when settings hex decode fails."""
        monkeypatch.setenv("AUDIT_LEDGER_HMAC_KEY", "env_secret_key_123")
        with patch.object(
            manager.settings.security, "app_master_key", "invalid_non_hex_string"
        ):
            key = manager._resolve_hmac_key()
            assert key == b"env_secret_key_123"

    def test_resolve_hmac_key_empty_fallback(self, monkeypatch):
        """Test fallback to empty bytes when settings fails and env var is not set."""
        monkeypatch.delenv("AUDIT_LEDGER_HMAC_KEY", raising=False)
        with patch.object(
            manager.settings.security, "app_master_key", "invalid_non_hex_string"
        ):
            key = manager._resolve_hmac_key()
            assert key == b""

    def test_resolve_path_default(self, monkeypatch):
        """Test default file path when AUDIT_LEDGER_PATH is not configured."""
        monkeypatch.delenv("AUDIT_LEDGER_PATH", raising=False)
        assert manager._resolve_path() == "telemetry/audit.log"

    def test_resolve_path_custom_env(self, monkeypatch):
        """Test custom path override via AUDIT_LEDGER_PATH environment variable."""
        monkeypatch.setenv("AUDIT_LEDGER_PATH", "/var/log/audit/custom.log")
        assert manager._resolve_path() == "/var/log/audit/custom.log"


# ============================================================================
# Shared Ledger Singleton Tests
# ============================================================================


@pytest.mark.unit
class TestTelemetryManagerSingleton:
    """Tests for get_shared_ledger singleton pattern and startup."""

    def test_get_shared_ledger_singleton_identity(self, tmp_path, monkeypatch):
        """Test get_shared_ledger creates AuditLedger on first call and reuses instance."""
        test_file = str(tmp_path / "shared.log")
        monkeypatch.setenv("AUDIT_LEDGER_PATH", test_file)

        with patch.object(AuditLedger, "start") as mock_start:
            ledger1 = manager.get_shared_ledger()
            assert isinstance(ledger1, AuditLedger)
            mock_start.assert_called_once()

            # Second retrieval returns exact same singleton
            ledger2 = manager.get_shared_ledger()
            assert ledger2 is ledger1
            # start() should not be invoked a second time
            mock_start.assert_called_once()

    def test_get_shared_ledger_handles_runtime_error_for_loop(
        self, tmp_path, monkeypatch
    ):
        """Test get_shared_ledger handles RuntimeError when getting event loop."""
        test_file = str(tmp_path / "shared.log")
        monkeypatch.setenv("AUDIT_LEDGER_PATH", test_file)

        with patch("asyncio.get_event_loop", side_effect=RuntimeError("No event loop")):
            with patch.object(AuditLedger, "start") as mock_start:
                ledger = manager.get_shared_ledger()
                assert isinstance(ledger, AuditLedger)
                mock_start.assert_called_once()


# ============================================================================
# Shutdown Tests
# ============================================================================


@pytest.mark.unit
class TestTelemetryManagerShutdown:
    """Tests for stop_shared_ledger across various event loop runtime conditions."""

    def test_stop_shared_ledger_when_none_is_noop(self):
        """Test stop_shared_ledger does nothing and does not raise when ledger is None."""
        assert manager._shared_ledger is None
        manager.stop_shared_ledger()
        assert manager._shared_ledger is None

    def test_stop_shared_ledger_running_event_loop(self):
        """Test stop_shared_ledger schedules task when event loop is running."""
        mock_ledger = MagicMock(spec=AuditLedger)
        mock_ledger.stop = MagicMock()
        manager._shared_ledger = mock_ledger

        mock_loop = MagicMock()
        mock_loop.is_running.return_value = True

        with patch("asyncio.get_event_loop", return_value=mock_loop):
            manager.stop_shared_ledger()
            mock_loop.create_task.assert_called_once()
            assert manager._shared_ledger is None

    def test_stop_shared_ledger_stopped_event_loop(self):
        """Test stop_shared_ledger calls run_until_complete when loop is not running."""
        mock_ledger = MagicMock(spec=AuditLedger)
        mock_ledger.stop = MagicMock()
        manager._shared_ledger = mock_ledger

        mock_loop = MagicMock()
        mock_loop.is_running.return_value = False

        with patch("asyncio.get_event_loop", return_value=mock_loop):
            manager.stop_shared_ledger()
            mock_loop.run_until_complete.assert_called_once()
            assert manager._shared_ledger is None

    def test_stop_shared_ledger_fallback_asyncio_run(self):
        """Test fallback to asyncio.run when get_event_loop fails."""
        mock_ledger = MagicMock(spec=AuditLedger)
        mock_ledger.stop = MagicMock()
        manager._shared_ledger = mock_ledger

        with patch("asyncio.get_event_loop", side_effect=RuntimeError("No loop")):
            with patch("asyncio.run") as mock_asyncio_run:
                manager.stop_shared_ledger()
                mock_asyncio_run.assert_called_once()
                assert manager._shared_ledger is None

    def test_stop_shared_ledger_swallows_all_exceptions(self):
        """Test stop_shared_ledger catches and logs any internal errors and clears ledger."""
        mock_ledger = MagicMock(spec=AuditLedger)
        manager._shared_ledger = mock_ledger

        with patch(
            "asyncio.get_event_loop", side_effect=Exception("Catastrophic error")
        ):
            with patch("asyncio.run", side_effect=Exception("asyncio.run failed too")):
                # Should not raise exception
                manager.stop_shared_ledger()
                assert manager._shared_ledger is None
