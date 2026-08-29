import unittest
import asyncio
import os
from services.media_handler import MediaHandler, MediaGroupBuffer

class DummyMessage:
    def __init__(self, msg_id: int, grouped_id: int):
        self.id = msg_id
        self.grouped_id = grouped_id
        self.media = None
        self.photo = None
        self.voice = None
        self.video_note = None
        self.video = None
        self.audio = None
        self.sticker = None
        self.gif = None
        self.document = None
        self.poll = None
        self.contact = None
        self.geo = None
        self.venue = None

class TestMediaHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.handler = MediaHandler(temp_dir="tests/temp_test_media")

    def tearDown(self):
        if os.path.exists("tests/temp_test_media"):
            try:
                os.rmdir("tests/temp_test_media")
            except Exception:
                pass

    def test_media_type_detection(self):
        msg = DummyMessage(1, 0)
        self.assertEqual(self.handler.get_media_type(msg), "text")

    async def test_media_group_buffer(self):
        buffer = MediaGroupBuffer(debounce_delay=0.2)
        flushed_groups = []

        async def callback(gid, msgs):
            flushed_groups.append((gid, [m.id for m in msgs]))

        msg1 = DummyMessage(101, 5555)
        msg2 = DummyMessage(102, 5555)
        msg3 = DummyMessage(103, 5555)

        buffer.add_message(5555, msg2, callback)
        buffer.add_message(5555, msg1, callback)
        buffer.add_message(5555, msg3, callback)

        # Wait for debounce timer
        await asyncio.sleep(0.35)

        self.assertEqual(len(flushed_groups), 1)
        gid, ids = flushed_groups[0]
        self.assertEqual(gid, 5555)
        self.assertEqual(ids, [101, 102, 103], "Messages should be ordered by ID")

if __name__ == "__main__":
    unittest.main()
