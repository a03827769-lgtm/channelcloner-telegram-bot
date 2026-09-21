import os
import sys
import json
import pytest
from unittest.mock import patch, MagicMock

# Import watchdog script module
import scripts.windows_keepalive_watchdog as watchdog


def test_watchdog_keep_awake_win32():
    """Verifies that enable_keep_awake and disable_keep_awake do not crash and call ctypes."""
    with patch("ctypes.windll.kernel32.SetThreadExecutionState", return_value=1) as mock_state:
        watchdog.enable_keep_awake()
        assert mock_state.called
        expected_flags = watchdog.ES_CONTINUOUS | watchdog.ES_SYSTEM_REQUIRED | watchdog.ES_AWAYMODE_REQUIRED
        assert mock_state.call_args[0][0] == expected_flags

        watchdog.disable_keep_awake()
        assert mock_state.call_args[0][0] == watchdog.ES_CONTINUOUS


def test_watchdog_single_instance_mutex():
    """Verifies that single instance mutex logic creates and releases handles cleanly."""
    with patch("ctypes.windll.kernel32.CreateMutexW", return_value=12345), \
         patch("ctypes.windll.kernel32.GetLastError", return_value=0), \
         patch("ctypes.windll.kernel32.ReleaseMutex") as mock_release, \
         patch("ctypes.windll.kernel32.CloseHandle") as mock_close:
        acquired = watchdog.acquire_single_instance_mutex()
        assert acquired is True
        assert watchdog._mutex_handle == 12345

        watchdog.release_single_instance_mutex()
        assert mock_release.called
        assert mock_close.called
        assert watchdog._mutex_handle is None


def test_watchdog_mutex_already_exists():
    """Verifies that acquire_single_instance_mutex returns False when mutex already exists."""
    ERROR_ALREADY_EXISTS = 183
    with patch("ctypes.windll.kernel32.CreateMutexW", return_value=12345), \
         patch("ctypes.windll.kernel32.GetLastError", return_value=ERROR_ALREADY_EXISTS), \
         patch("ctypes.windll.kernel32.CloseHandle") as mock_close:
        acquired = watchdog.acquire_single_instance_mutex()
        assert acquired is False
        assert mock_close.called


def test_watchdog_health_url_resolution(tmp_path):
    """Verifies health URL resolution from port file and environment fallback."""
    # Test default
    with patch.dict(os.environ, {}, clear=True), \
         patch("os.path.exists", return_value=False):
        url = watchdog.get_health_url()
        assert url == "http://127.0.0.1:8080/health"

    # Test env var
    with patch.dict(os.environ, {"PORT": "8888"}):
        url = watchdog.get_health_url()
        assert url == "http://127.0.0.1:8888/health"


def test_watchdog_save_status(tmp_path):
    """Verifies status payload is serializable and written cleanly."""
    status_file = tmp_path / "watchdog_status.json"
    with patch.object(watchdog, "STATUS_FILE", str(status_file)):
        watchdog.save_status("docker", True, 0)
        assert status_file.exists()
        with open(status_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert data["mode"] == "docker"
            assert data["healthy"] is True
            assert data["win32_away_mode"] is True
            assert data["consecutive_failures"] == 0


def test_watchdog_network_keepalive_no_exception():
    """Verifies keep_network_alive runs safely without unhandled exceptions."""
    with patch("socket.getaddrinfo", return_value=[]):
        watchdog.keep_network_alive()
