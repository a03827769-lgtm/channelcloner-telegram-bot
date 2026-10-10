import asyncio
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from telethon.errors import FileReferenceExpiredError

from bot.error_handler import USER_ERROR_TEXT, handle_update_error, is_benign_bad_request
from services.media_handler import MediaHandler
from services.story_queue_service import (
    CLIENT_MAX_RETRIES,
    CLIENT_RETRY_MAX_MINUTES,
    client_retry_delay_minutes,
)

_METHOD = SendMessage(chat_id=1, text="x")


def _expired() -> FileReferenceExpiredError:
    return FileReferenceExpiredError(request=None)


class _FakeMessage:
    """Telethon message stand-in whose download fails with an expired file reference"""

    def __init__(self, msg_id: int, client, fail: bool = True):
        self.id = msg_id
        self.client = client
        self.peer_id = "peer"
        self.media = object()
        self.photo = object()
        self.file = SimpleNamespace(name=None, ext=".jpg", size=1024)
        self.fail = fail
        self.download_calls = 0

    async def download_media(self, file=None, **_):
        self.download_calls += 1
        if self.fail:
            raise _expired()
        if hasattr(file, "write"):
            file.write(b"jpeg-bytes")
            return file
        with open(file, "wb") as fh:
            fh.write(b"jpeg-bytes")
        return file


# --- media: expired file references are refreshed once ---

async def test_disk_download_refetches_message_on_expired_file_reference(tmp_path):
    handler = MediaHandler(temp_dir=str(tmp_path))
    client = MagicMock()
    stale = _FakeMessage(7, client, fail=True)
    fresh = _FakeMessage(7, client, fail=False)
    client.get_messages = AsyncMock(return_value=fresh)

    path = await handler.download_telethon_media(stale)

    assert path and os.path.exists(path)
    client.get_messages.assert_awaited_once_with("peer", ids=7)
    assert stale.download_calls == 1 and fresh.download_calls == 1
    await handler.cleanup_files([path])


async def test_disk_download_refreshes_only_once(tmp_path):
    handler = MediaHandler(temp_dir=str(tmp_path))
    client = MagicMock()
    stale = _FakeMessage(8, client, fail=True)
    still_stale = _FakeMessage(8, client, fail=True)
    client.get_messages = AsyncMock(return_value=still_stale)

    assert await handler.download_telethon_media(stale) is None
    client.get_messages.assert_awaited_once()
    assert os.listdir(tmp_path) == []


async def test_bytes_download_refetches_message_on_expired_file_reference(tmp_path):
    handler = MediaHandler(temp_dir=str(tmp_path))
    client = MagicMock()
    stale = _FakeMessage(9, client, fail=True)
    fresh = _FakeMessage(9, client, fail=False)
    client.get_messages = AsyncMock(return_value=fresh)

    assert await handler.download_telethon_media_bytes(stale) == b"jpeg-bytes"
    client.get_messages.assert_awaited_once_with("peer", ids=9)


async def test_refetch_returns_none_when_message_is_gone(tmp_path):
    client = MagicMock()
    client.get_messages = AsyncMock(return_value=None)
    assert await MediaHandler.refetch_message(_FakeMessage(10, client)) is None

    client.get_messages = AsyncMock(side_effect=ConnectionError("down"))
    assert await MediaHandler.refetch_message(_FakeMessage(11, client)) is None


# --- story queue: client outages back off instead of failing within 50 minutes ---

def test_client_retry_delay_backs_off_and_caps():
    delays = [client_retry_delay_minutes(n) for n in range(1, CLIENT_MAX_RETRIES)]
    assert delays[:5] == [5, 10, 20, 40, 60]
    assert max(delays) == CLIENT_RETRY_MAX_MINUTES
    # All postponements together cover a multi-hour power or network outage
    assert sum(delays) >= 6 * 60
    assert client_retry_delay_minutes(0) == 5


# --- aiogram error handler ---

def _event(exc, *, callback=None, message=None):
    update = SimpleNamespace(update_id=1, callback_query=callback, message=message)
    return SimpleNamespace(exception=exc, update=update)


async def test_blocked_user_is_handled_quietly(caplog):
    caplog.set_level(logging.INFO, logger="bot.error_handler")
    message = SimpleNamespace(chat=SimpleNamespace(type="private"), answer=AsyncMock())
    exc = TelegramForbiddenError(method=_METHOD, message="Forbidden: bot was blocked by the user")

    assert await handle_update_error(_event(exc, message=message)) is True
    message.answer.assert_not_awaited()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_benign_bad_request_and_network_errors_are_handled():
    exc = TelegramBadRequest(method=_METHOD, message="Bad Request: message is not modified")
    assert is_benign_bad_request(exc)
    assert not is_benign_bad_request(TelegramBadRequest(method=_METHOD, message="Bad Request: chat not found"))
    assert await handle_update_error(_event(exc)) is True
    assert await handle_update_error(_event(TelegramNetworkError(method=_METHOD, message="timeout"))) is True


async def test_unexpected_error_answers_callback_with_alert(caplog):
    callback = SimpleNamespace(answer=AsyncMock())
    assert await handle_update_error(_event(RuntimeError("boom"), callback=callback)) is True
    callback.answer.assert_awaited_once_with(USER_ERROR_TEXT, show_alert=True)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_unexpected_error_replies_only_in_private_chats():
    private = SimpleNamespace(chat=SimpleNamespace(type="private"), answer=AsyncMock())
    await handle_update_error(_event(ValueError("x"), message=private))
    private.answer.assert_awaited_once_with(USER_ERROR_TEXT)

    group = SimpleNamespace(chat=SimpleNamespace(type="supergroup"), answer=AsyncMock())
    await handle_update_error(_event(ValueError("x"), message=group))
    group.answer.assert_not_awaited()


async def test_error_handler_survives_failed_notice():
    callback = SimpleNamespace(answer=AsyncMock(side_effect=RuntimeError("already answered")))
    assert await handle_update_error(_event(KeyError("k"), callback=callback)) is True


def test_both_dispatchers_register_the_error_handler():
    from admin_bot.bot_instance import create_admin_dispatcher
    from bot.bot_instance import create_dispatcher

    from aiogram.fsm.storage.memory import MemoryStorage

    for dp in (create_dispatcher(storage=MemoryStorage()), create_admin_dispatcher(storage=MemoryStorage())):
        callbacks = [h.callback for h in dp.errors.handlers]
        assert handle_update_error in callbacks


# --- asyncio: Telethon's unretrieved reconnect futures are one log line, not a traceback ---

def test_loop_exception_handler_downgrades_network_futures(caplog):
    import run

    loop = MagicMock()
    run.loop_exception_handler(loop, {
        "message": "Future exception was never retrieved",
        "exception": ConnectionError("Connection to Telegram failed 5 time(s)"),
    })
    loop.default_exception_handler.assert_not_called()

    other = {"message": "Task exception was never retrieved", "exception": ValueError("bug")}
    run.loop_exception_handler(loop, other)
    loop.default_exception_handler.assert_called_once_with(other)


async def test_loop_exception_handler_works_on_a_real_loop():
    import run

    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(run.loop_exception_handler)
    try:
        loop.call_exception_handler({
            "message": "Future exception was never retrieved",
            "exception": TimeoutError(),
        })
    finally:
        loop.set_exception_handler(previous)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
