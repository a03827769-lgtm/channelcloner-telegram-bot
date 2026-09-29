"""
Windows keep-awake watchdog: Win32 keep-awake, single-instance mutex, health URL resolution, status file.

Windows-only (Win32 APIs). Every path the watchdog reads or writes is redirected to a temporary directory,
and the Win32 mutex is mocked, so the running production watchdog is never touched.
"""
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only watchdog (Win32 keep-awake / mutex)")

import scripts.windows_keepalive_watchdog as watchdog  # noqa: E402


def test_watchdog_keep_awake_win32():
    """enable_keep_awake / disable_keep_awake set the expected execution-state flags."""
    with patch("ctypes.windll.kernel32.SetThreadExecutionState", return_value=1) as mock_state:
        watchdog.enable_keep_awake()
        assert mock_state.called
        expected_flags = watchdog.ES_CONTINUOUS | watchdog.ES_SYSTEM_REQUIRED | watchdog.ES_AWAYMODE_REQUIRED
        assert mock_state.call_args[0][0] == expected_flags

        watchdog.disable_keep_awake()
        assert mock_state.call_args[0][0] == watchdog.ES_CONTINUOUS


def _fake_kernel32(handle):
    kernel32 = MagicMock()
    kernel32.CreateMutexW.return_value = handle
    return kernel32


def test_watchdog_single_instance_mutex():
    """A fresh mutex is acquired and released/closed cleanly."""
    kernel32 = _fake_kernel32(12345)
    with patch.object(watchdog, "_kernel32", return_value=kernel32), \
         patch("ctypes.get_last_error", return_value=0):
        assert watchdog.acquire_single_instance_mutex() is True
        assert watchdog._mutex_handle == 12345

        watchdog.release_single_instance_mutex()
        kernel32.ReleaseMutex.assert_called_once_with(12345)
        kernel32.CloseHandle.assert_called_once_with(12345)
        assert watchdog._mutex_handle is None


@pytest.mark.parametrize("last_error", [watchdog.ERROR_ALREADY_EXISTS, watchdog.ERROR_ACCESS_DENIED])
def test_watchdog_mutex_already_exists(last_error):
    """ERROR_ALREADY_EXISTS and ERROR_ACCESS_DENIED (e.g. an elevated watchdog) both mean another instance."""
    kernel32 = _fake_kernel32(12345)
    with patch.object(watchdog, "_kernel32", return_value=kernel32), \
         patch("ctypes.get_last_error", return_value=last_error):
        assert watchdog.acquire_single_instance_mutex() is False
        kernel32.CloseHandle.assert_called_once_with(12345)
    assert watchdog._mutex_handle is None


def test_watchdog_mutex_null_handle_is_another_instance():
    """CreateMutexW returning NULL (access denied to an existing mutex) is never treated as 'acquired'."""
    kernel32 = _fake_kernel32(0)
    with patch.object(watchdog, "_kernel32", return_value=kernel32), \
         patch("ctypes.get_last_error", return_value=watchdog.ERROR_ACCESS_DENIED):
        assert watchdog.acquire_single_instance_mutex() is False
        kernel32.CloseHandle.assert_not_called()
    assert watchdog._mutex_handle is None


def test_watchdog_health_url_resolution(tmp_path, monkeypatch):
    """Port priority: bound port file > PORT env > PORT in .env > 8080 (isolated from the live logs/)."""
    monkeypatch.setattr(watchdog, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(watchdog, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("PORT", raising=False)
    assert watchdog.get_health_url() == "http://127.0.0.1:8080/health"

    monkeypatch.setenv("PORT", "8888")
    assert watchdog.get_health_url() == "http://127.0.0.1:8888/health"

    monkeypatch.delenv("PORT")
    (tmp_path / ".env").write_text("BOT_TOKEN=x\nPORT=8123\n", encoding="utf-8")
    assert watchdog.get_health_url() == "http://127.0.0.1:8123/health"

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "health_port.txt").write_text("8095", encoding="utf-8")
    monkeypatch.setenv("PORT", "8888")
    assert watchdog.get_health_url() == "http://127.0.0.1:8095/health"


def test_watchdog_save_status(tmp_path):
    """Status payload is serializable and written cleanly (atomically)."""
    status_file = tmp_path / "watchdog_status.json"
    with patch.object(watchdog, "STATUS_FILE", str(status_file)):
        watchdog.save_status("native", True, 0)
        assert status_file.exists()
        with open(status_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert data["mode"] == "native"
            assert data["healthy"] is True
            assert data["win32_away_mode"] is True
            assert data["consecutive_failures"] == 0
    assert [p.name for p in tmp_path.iterdir()] == ["watchdog_status.json"]


def test_watchdog_network_keepalive_no_exception():
    """keep_network_alive runs safely without unhandled exceptions."""
    with patch("socket.getaddrinfo", return_value=[]):
        watchdog.keep_network_alive()
