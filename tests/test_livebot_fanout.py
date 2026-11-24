"""MPEG-TS fan-out: GOP cache for new viewers, slow viewers, limits, end of stream."""

from __future__ import annotations

import io
import queue
import unittest

try:
    from test_recorder_reliability import IsolatedBaseTest  # noqa: F401  (sets VOD_* env first)
except ImportError:  # run as tests.test_livebot_fanout
    from tests.test_recorder_reliability import IsolatedBaseTest  # noqa: F401

from recorder.ts_fanout import CHUNK_SIZE, MpegTsBroadcaster  # noqa: E402

PACKETS_PER_CHUNK = CHUNK_SIZE // 188
VIDEO_PID, AUDIO_PID, PMT_PID = 256, 257, 4096


def _pkt(pid, *, pusi=False, af=b"", payload=b""):
    afc = 3 if af else 1
    head = bytes([0x47, (0x40 if pusi else 0) | (pid >> 8), pid & 0xFF, afc << 4])
    body = (bytes([len(af)]) + af if af else b"") + payload
    return (head + body).ljust(188, b"\xff")[:188]


def pat():
    section = bytes([0x00, 0xB0, 0x0D, 0x00, 0x01, 0xC1, 0x00, 0x00, 0x00, 0x01,
                     0xF0 | (PMT_PID >> 8), PMT_PID & 0xFF, 0, 0, 0, 0])
    return _pkt(0, pusi=True, payload=b"\x00" + section)


def pmt():
    return _pkt(PMT_PID, pusi=True, payload=b"\x00\x02\xb0\x17PMT")


def video_start(key):
    pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + b"\x21\x00\x01\x00\x01"
    return _pkt(VIDEO_PID, pusi=True, af=b"\x40" if key else b"", payload=pes)


def video_cont(fill):
    return _pkt(VIDEO_PID, payload=bytes([fill]) * 184)


def plain_chunk(fill):
    audio = _pkt(AUDIO_PID, pusi=True, payload=b"\x00\x00\x01\xc0" + bytes([fill]) * 20)
    return b"".join([video_cont(fill)] * (PACKETS_PER_CHUNK - 7) + [audio] * 7)


def key_chunk(fill, key_at=10, with_tables=True):
    packets = [video_cont(fill)] * key_at
    if with_tables:
        packets += [pat(), pmt()]
    packets.append(video_start(True))
    packets += [video_cont(fill)] * (PACKETS_PER_CHUNK - len(packets))
    return b"".join(packets)


class FanoutTests(unittest.TestCase):
    def _run(self, broadcaster, chunks):
        broadcaster.start(io.BufferedReader(io.BytesIO(b"".join(chunks))))
        broadcaster.thread.join(timeout=5)
        self.assertFalse(broadcaster.thread.is_alive())
        return broadcaster

    def _broadcast(self, chunks):
        return self._run(MpegTsBroadcaster("demochan"), chunks)

    @staticmethod
    def _drain(viewer):
        items = []
        while True:
            try:
                items.append(viewer.get_nowait())
            except queue.Empty:
                return items

    def test_late_viewer_starts_at_last_keyframe_with_tables(self):
        k1, p1, k2, p2, p3 = key_chunk(1), plain_chunk(2), key_chunk(3, key_at=20), plain_chunk(4), plain_chunk(5)
        items = self._drain(self._broadcast([k1, p1, k2, p2, p3]).subscribe())
        self.assertEqual(items[0], pat() + pmt())
        self.assertEqual(items[1][:188], video_start(True))
        self.assertEqual(items[1], k2[22 * 188:])
        self.assertEqual(items[2:], [p2, p3])

    def test_no_keyframe_or_no_tables_means_no_replay(self):
        # Replaying the first chunk of a pipeline made new viewers start with a huge
        # timestamp jump and broken audio packets.
        for chunks in ([plain_chunk(1), plain_chunk(2)], [key_chunk(1, with_tables=False), plain_chunk(2)]):
            self.assertEqual(self._drain(self._broadcast(chunks).subscribe()), [])

    def test_cache_is_dropped_when_keyframes_stop(self):
        limit = MpegTsBroadcaster.GOP_CACHE_MAX_CHUNKS
        chunks = [key_chunk(1)] + [plain_chunk(i % 200) for i in range(limit + 5)]
        self.assertEqual(self._drain(self._broadcast(chunks).subscribe()), [])

    def test_replay_fits_into_viewer_queue(self):
        limit = MpegTsBroadcaster.GOP_CACHE_MAX_CHUNKS
        self.assertLess(limit + 1, MpegTsBroadcaster.QUEUE_MAXSIZE)
        viewer = self._broadcast([key_chunk(1)] + [plain_chunk(i % 200) for i in range(limit - 1)]).subscribe()
        self.assertEqual(len(self._drain(viewer)), 1 + limit)

    def test_viewer_receives_live_chunks_in_order_and_the_end(self):
        broadcaster = MpegTsBroadcaster("demochan")
        viewer = broadcaster.subscribe()
        chunks = [plain_chunk(1), plain_chunk(2), plain_chunk(3)]
        self._run(broadcaster, chunks)
        self.assertEqual(self._drain(viewer), chunks + [None])
        self.assertEqual(broadcaster.active_viewers(), 0)

    def test_slow_viewer_loses_oldest_chunks_and_still_gets_the_end(self):
        broadcaster = MpegTsBroadcaster("demochan")
        viewer = broadcaster.subscribe()
        chunks = [plain_chunk(i % 250) for i in range(viewer.maxsize + 5)]
        with self.assertLogs("livebot", level="WARNING") as logs:
            self._run(broadcaster, chunks)
        self.assertTrue(any("dropped" in line and "demochan" in line for line in logs.output), logs.output)
        items = self._drain(viewer)
        self.assertEqual(items[-2:], [chunks[-1], None])

    def test_viewer_limit(self):
        broadcaster = MpegTsBroadcaster("demochan")
        first = broadcaster.subscribe(2)
        self.assertIsNotNone(broadcaster.subscribe(2))
        self.assertIsNone(broadcaster.subscribe(2))
        broadcaster.unsubscribe(first)
        self.assertIsNotNone(broadcaster.subscribe(2))

    def test_broken_pipe_is_logged_not_raised(self):
        class Broken(io.RawIOBase):
            def read(self, *_args):
                raise OSError("pipe gone")

        broadcaster = MpegTsBroadcaster("demochan")
        with self.assertLogs("livebot", level="WARNING"):
            broadcaster.start(Broken())
            broadcaster.thread.join(timeout=5)
        self.assertFalse(broadcaster.running)


if __name__ == "__main__":
    unittest.main()
