import os
import time
import asyncio
import tempfile
import shutil
import pytest
from services.code_reloader import SourceCodeWatcher
from scripts.windows_keepalive_watchdog import CodeChangeWatcher


def test_source_code_watcher_scan_and_ignore():
    temp_dir = tempfile.mkdtemp()
    try:
        bot_dir = os.path.join(temp_dir, "bot")
        os.makedirs(bot_dir, exist_ok=True)
        py_file = os.path.join(bot_dir, "test_handler.py")
        with open(py_file, "w") as f:
            f.write("# test")

        ignored_dir = os.path.join(temp_dir, "data")
        os.makedirs(ignored_dir, exist_ok=True)
        db_file = os.path.join(ignored_dir, "cloner.db")
        with open(db_file, "w") as f:
            f.write("db")

        watcher = SourceCodeWatcher(base_dir=temp_dir, poll_interval=0.1, debounce_seconds=0.2)
        scanned = watcher.scan_mtimes()

        # py_file should be tracked
        assert py_file in scanned
        # db_file in data/ should NOT be tracked
        assert db_file not in scanned
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_code_change_watcher_detects_modification():
    temp_dir = tempfile.mkdtemp()
    try:
        serv_dir = os.path.join(temp_dir, "services")
        os.makedirs(serv_dir, exist_ok=True)
        mod_file = os.path.join(serv_dir, "test_service.py")
        with open(mod_file, "w") as f:
            f.write("print('v1')")

        changes_received = []

        def on_change(changed):
            changes_received.extend(changed)

        watcher = CodeChangeWatcher(base_dir=temp_dir, on_change_callback=on_change, debounce_seconds=0.3, poll_interval=0.1)
        watcher.start()

        time.sleep(0.3)
        # Modify the file
        with open(mod_file, "w") as f:
            f.write("print('v2')")

        # Wait for scan + debounce
        time.sleep(0.8)
        watcher.stop()

        assert len(changes_received) >= 1
        assert any("test_service.py" in c for c in changes_received)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_source_code_watcher_async_callback():
    temp_dir = tempfile.mkdtemp()
    try:
        bot_dir = os.path.join(temp_dir, "bot")
        os.makedirs(bot_dir, exist_ok=True)
        target = os.path.join(bot_dir, "watcher_test.py")
        with open(target, "w") as f:
            f.write("# v1")

        received = []
        watcher = SourceCodeWatcher(base_dir=temp_dir, poll_interval=0.1, debounce_seconds=0.2)
        watcher.add_reload_callback(lambda items: received.extend(items))
        watcher.start()

        await asyncio.sleep(0.2)
        with open(target, "w") as f:
            f.write("# v2")

        await asyncio.sleep(0.6)
        watcher.stop()

        assert len(received) >= 1
        assert any("watcher_test.py" in x for x in received)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
