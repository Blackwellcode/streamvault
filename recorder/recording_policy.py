"""Decisions of the recording loop, free of I/O.

``record_stream`` in Streamrecorder.py drives processes, files and status
updates; everything it has to *decide* lives here: how long to wait after an
offline probe, an unplayable stream or a failed FFmpeg run, when to fall back
from copy mode to transcoding, which error text to show, and whether a
finished file may be offloaded to cloud storage.

The policy keeps the per-channel counters. Delays come from injected
functions so tests get deterministic values (production adds jitter).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

STATE_STARTING = "starting"
STATE_OFFLINE = "offline"


@dataclass
class RecordingPolicy:
    backoff: Callable[[int], int]
    """failures (>= 1) -> seconds; exponential in production."""
    idle_delay: Callable[[], int]
    """Fixed poll interval while a channel is offline."""
    unplayable_delay: int
    """Retry interval while a channel is live but not yet playable."""

    offline_failures: int = 0
    recording_failures: int = 0
    playable_failures: int = 0
    copy_fallback: bool = False

    # --- before a recording starts -------------------------------------
    @staticmethod
    def effective_presence(is_live: Optional[bool], in_restart_grace: bool) -> Optional[bool]:
        """During the restart grace an "offline" verdict is treated as unknown."""
        if is_live is False and in_restart_grace:
            return None
        return is_live

    def on_disk_full(self) -> int:
        return self.backoff(max(1, self.recording_failures + 1))

    def on_offline(self) -> int:
        self.offline_failures += 1
        self.playable_failures = 0
        self.recording_failures = 0
        return self.idle_delay()

    def on_unplayable(self, is_live: Optional[bool], keep_trying: bool) -> tuple[str, int]:
        """Probe found no playable stream. Live (or in grace) -> keep polling fast."""
        if is_live is True or keep_trying:
            self.playable_failures += 1
            return STATE_STARTING, max(1, int(self.unplayable_delay))
        self.offline_failures += 1
        return STATE_OFFLINE, self.idle_delay()

    def on_playable(self) -> None:
        self.offline_failures = 0
        self.playable_failures = 0

    def use_copy(self, copy_mode: bool, use_vaapi: bool) -> bool:
        """Copy (remux-only) mode unless VAAPI is active or copy already failed."""
        return bool(copy_mode and not use_vaapi and not self.copy_fallback)

    # --- after FFmpeg exits --------------------------------------------
    def on_completed(self) -> None:
        self.recording_failures = 0
        self.copy_fallback = False

    def on_empty_output(self, used_copy: bool) -> int:
        """Exit code 0 but no usable file."""
        if used_copy:
            self.copy_fallback = True
        self.recording_failures += 1
        return self.backoff(self.recording_failures)

    def on_ffmpeg_failed(self, used_copy: bool, temp_size: int, min_size: int) -> int:
        """Non-zero exit. A copy run that produced (almost) nothing switches to transcode."""
        if used_copy and temp_size < min_size:
            self.copy_fallback = True
        self.recording_failures += 1
        return self.backoff(self.recording_failures)

    def on_exception(self) -> int:
        self.recording_failures += 1
        return self.backoff(self.recording_failures)

    @staticmethod
    def error_message(returncode: int, source_hint: str) -> str:
        message = f"FFmpeg Fehlercode {returncode}"
        hint = source_hint or ""
        if "Requested format is not available" in hint:
            return "YouTube-Format nicht verfuegbar (live-from-start/DASH?)"
        if ("Invalid data found" in hint or returncode == 183) and "format" in hint.lower():
            return "YouTube-Quelle lieferte kein gueltiges Stream-Format"
        return message

    @staticmethod
    def upload_now(*, integrity_ok: bool, auto_remux: bool, sync_remux: bool, short_recording: bool) -> bool:
        """Offload a finished file now?

        Never a file that failed integrity validation. When an asynchronous
        remux will replace the file, the periodic sync uploads the final one.
        """
        if not integrity_ok:
            return False
        return not (auto_remux and not sync_remux and not short_recording)
