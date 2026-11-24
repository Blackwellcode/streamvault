"""Fan-out of one MPEG-TS byte stream to many viewers (used by livebot).

Fast start: the hub keeps everything since the last video keyframe (one GOP,
about 2 s on Twitch) plus the current PAT/PMT packets. A new viewer gets both
at once and neither waits for the next keyframe nor sees older data.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

logger = logging.getLogger("livebot")

PACKET = 188
CHUNK_SIZE = PACKET * 87


class MpegTsBroadcaster:
    GOP_CACHE_MAX_CHUNKS = 192  # about 3 MB; longer without a keyframe drops the cache
    QUEUE_MAXSIZE = 256  # always larger than tables + cached GOP
    DROP_LOG_INTERVAL_SEC = 10

    def __init__(self, channel):
        self.channel = channel
        self.lock = threading.Lock()
        self.subscribers = set()
        self.running = False
        self.thread = None
        self.total_bytes = 0
        self.dropped_chunks = 0
        self.first_byte_time = 0.0
        self.last_chunk_time = 0.0
        self._last_drop_log = 0.0
        self._gop = []
        self._pat_packet = None
        self._pmt_packet = None
        self._pmt_pid = None

    def start(self, stdout_pipe):
        self.running = True
        self.thread = threading.Thread(
            target=self._reader_loop, args=(stdout_pipe,), daemon=True, name=f"ts-fanout-{self.channel}"
        )
        self.thread.start()

    def subscribe(self, max_viewers=None):
        """Queue of TS chunks for one viewer; None when the viewer limit is reached."""
        viewer = queue.Queue(maxsize=self.QUEUE_MAXSIZE)
        with self.lock:
            if max_viewers is not None and len(self.subscribers) >= max_viewers:
                return None
            # Same lock as the reader: a chunk lands either in the replay or in the
            # live feed of this viewer, never in both or neither.
            if self._gop and self._pat_packet and self._pmt_packet:
                viewer.put_nowait(self._pat_packet + self._pmt_packet)
                for cached in self._gop:
                    viewer.put_nowait(cached)
            self.subscribers.add(viewer)
        return viewer

    def unsubscribe(self, viewer):
        with self.lock:
            self.subscribers.discard(viewer)

    def active_viewers(self):
        with self.lock:
            return len(self.subscribers)

    def stop(self):
        """End every viewer connection; the reader ends with its pipe."""
        self.running = False
        with self.lock:
            for viewer in self.subscribers:
                self._put_latest(viewer, None)
            self.subscribers.clear()

    def _put_latest(self, viewer, item):
        """Enqueue; a full queue loses its oldest entry, never the newest.

        Runs under ``self.lock``, so there is no other producer and the slot freed
        here cannot be taken again.
        """
        try:
            viewer.put_nowait(item)
            return
        except queue.Full:
            self.dropped_chunks += 1
        try:
            viewer.get_nowait()
        except queue.Empty:
            logger.debug("Viewer queue of %s drained while dropping", self.channel)
        viewer.put_nowait(item)

    def _reader_loop(self, stdout_pipe):
        try:
            while self.running:
                chunk = stdout_pipe.read(CHUNK_SIZE)
                if not chunk:
                    break
                self._publish(chunk, time.time())
        except (OSError, ValueError) as exc:  # ValueError: pipe already closed
            logger.warning("TS reader for %s ended: %s", self.channel, exc)
        finally:
            self.stop()
            try:
                stdout_pipe.close()
            except OSError as exc:
                logger.warning("TS pipe of %s not closed: %s", self.channel, exc)
            logger.info("TS reader for %s ended after %s bytes", self.channel, self.total_bytes)

    def _publish(self, chunk, now):
        if not self.first_byte_time:
            self.first_byte_time = now
        self.last_chunk_time = now
        self.total_bytes += len(chunk)
        key_offset = self._scan_chunk(chunk)
        with self.lock:
            # Never replay the first chunk of a pipeline (its timestamps can be hours
            # old), only the current GOP.
            if key_offset >= 0:
                self._gop = [chunk[key_offset:]]
            elif len(self._gop) >= self.GOP_CACHE_MAX_CHUNKS:
                self._gop = []
            elif self._gop:
                self._gop.append(chunk)
            for viewer in self.subscribers:
                self._put_latest(viewer, chunk)
        if self.dropped_chunks and now - self._last_drop_log >= self.DROP_LOG_INTERVAL_SEC:
            logger.warning(
                "Live %s: dropped %s chunks for slow viewers", self.channel, self.dropped_chunks
            )
            self.dropped_chunks = 0
            self._last_drop_log = now

    def _scan_chunk(self, chunk):
        """Remember PAT/PMT; return the offset of the first video keyframe or -1.

        Keyframe = PES start of a video stream (stream_id 0xE0-0xEF) with the
        random_access_indicator set; ffmpeg sets it exactly on keyframes.
        """
        key_offset = -1
        for off in range(0, len(chunk) - (PACKET - 1), PACKET):
            if chunk[off] != 0x47 or not chunk[off + 1] & 0x40:
                continue
            pid = ((chunk[off + 1] & 0x1F) << 8) | chunk[off + 2]
            if pid == 0:
                self._pat_packet = chunk[off:off + PACKET]
                section = off + 5 + chunk[off + 4]
                if section + 12 <= off + PACKET:
                    self._pmt_pid = ((chunk[section + 10] & 0x1F) << 8) | chunk[section + 11]
                continue
            if pid == self._pmt_pid:
                self._pmt_packet = chunk[off:off + PACKET]
                continue
            if (chunk[off + 3] >> 4) & 3 != 3:  # no adaptation field, so no random_access_indicator
                continue
            field_len = chunk[off + 4]
            payload = off + 5 + field_len
            if payload + 4 > off + PACKET or chunk[payload:payload + 3] != b"\x00\x00\x01":
                continue
            if key_offset < 0 and field_len and chunk[off + 5] & 0x40 and 0xE0 <= chunk[payload + 3] <= 0xEF:
                key_offset = off
        return key_offset
