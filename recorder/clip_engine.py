#!/usr/bin/env python3
"""Unified clip capture, encoding, and job queue."""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque

from core import config

logger = logging.getLogger(__name__)

CLIP_SUBDIR = os.environ.get("VOD_CLIP_SUBDIR", "clips")
SETTINGS_NAME = "clip_settings.json"
JOBS_SUBDIR = "clip_jobs"
JOB_PERSIST_TTL_SEC = 6 * 3600

DEFAULT_PREROLL_SEC = 30
DEFAULT_POSTROLL_SEC = 10
LIVE_INSTANT_DURATION_SEC = 60
DEFAULT_HOTKEY = "F8"
DEFAULT_QUALITY = "fast"
MIN_PREROLL_SEC = 5
MAX_PREROLL_SEC = 120
MIN_POSTROLL_SEC = 0
MAX_POSTROLL_SEC = 30
MAX_DURATION_SEC = 300
MIN_FREE_BYTES = 200 * 1024 * 1024
MAX_QUEUE = 8
DEBOUNCE_SEC = 0.45
FFMPEG_TIMEOUT_SEC = 300

_LOCK = threading.RLock()
_CV = threading.Condition(_LOCK)
_JOBS: dict[str, dict] = {}
_QUEUE: deque[str] = deque()
_LAST_INSTANT: dict[str, tuple[float, str]] = {}
_WORKER: threading.Thread | None = None
_STOP = threading.Event()
_ENCODE_LOCK = threading.Lock()
_ENCODER_CACHE: dict | None = None
_TEST_SYNC = False


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _clamp_int(value, lo: int, hi: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def settings_path() -> str:
    return os.path.join(config.META_DIR, SETTINGS_NAME)


def default_settings() -> dict:
    return {
        "preroll_sec": _clamp_int(
            _int_env("CLIP_PREROLL_SEC", DEFAULT_PREROLL_SEC),
            MIN_PREROLL_SEC,
            MAX_PREROLL_SEC,
            DEFAULT_PREROLL_SEC,
        ),
        "postroll_sec": _clamp_int(
            _int_env("CLIP_POSTROLL_SEC", DEFAULT_POSTROLL_SEC),
            MIN_POSTROLL_SEC,
            MAX_POSTROLL_SEC,
            DEFAULT_POSTROLL_SEC,
        ),
        "hotkey": (os.environ.get("CLIP_HOTKEY") or DEFAULT_HOTKEY).strip() or DEFAULT_HOTKEY,
        "quality": (os.environ.get("CLIP_QUALITY") or DEFAULT_QUALITY).strip().lower() or DEFAULT_QUALITY,
        "include_audio": True,
    }


def normalize_settings(data) -> dict:
    settings = default_settings()
    if not isinstance(data, dict):
        return settings
    settings["preroll_sec"] = _clamp_int(
        data.get("preroll_sec"), MIN_PREROLL_SEC, MAX_PREROLL_SEC, settings["preroll_sec"]
    )
    settings["postroll_sec"] = _clamp_int(
        data.get("postroll_sec"), MIN_POSTROLL_SEC, MAX_POSTROLL_SEC, settings["postroll_sec"]
    )
    hotkey = str(data.get("hotkey") or settings["hotkey"]).strip()[:16]
    settings["hotkey"] = hotkey or DEFAULT_HOTKEY
    quality = str(data.get("quality") or settings["quality"]).strip().lower()
    settings["quality"] = quality if quality in ("fast", "high") else DEFAULT_QUALITY
    settings["include_audio"] = bool(data.get("include_audio", True))
    return settings


def load_settings() -> dict:
    try:
        with open(settings_path(), "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except Exception:
        stored = {}
    return normalize_settings(stored)


def save_settings(data) -> dict:
    settings = normalize_settings(data)
    path = settings_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return settings


def public_settings() -> dict:
    settings = load_settings()
    return {
        "preroll_sec": settings["preroll_sec"],
        "postroll_sec": settings["postroll_sec"],
        "hotkey": settings["hotkey"],
        "quality": settings["quality"],
        "include_audio": settings["include_audio"],
        "duration_sec": settings["preroll_sec"] + settings["postroll_sec"],
    }


def ffmpeg_bin() -> str:
    return shutil.which("ffmpeg") or ""


def ffprobe_bin() -> str:
    return shutil.which("ffprobe") or ""


def _ffmpeg_cmd() -> str:
    return ffmpeg_bin() or "ffmpeg"


def _ffprobe_cmd() -> str:
    return ffprobe_bin() or "ffprobe"


def check_ffmpeg() -> tuple[bool, str]:
    if ffmpeg_bin():
        return True, ""
    return False, "FFmpeg ist nicht installiert."


def check_disk_space(path: str, min_bytes: int = MIN_FREE_BYTES) -> tuple[bool, str]:
    probe = path if os.path.exists(path) else os.path.dirname(path) or "."
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return False, "Speicherpfad ist nicht erreichbar."
    if usage.free < min_bytes:
        return False, "Nicht genug Speicherplatz für den Clip."
    return True, ""


def sanitize_label(value: str, max_len: int = 40) -> str:
    parts = []
    for ch in str(value or "").strip():
        if ch.isalnum() or ch in ("-", "_"):
            parts.append(ch)
        elif ch.isspace():
            parts.append("_")
    return "".join(parts).strip("_")[:max_len]


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    for index in range(2, 100):
        candidate = f"{base}_{index}{ext}"
        if not os.path.exists(candidate):
            return candidate
    return f"{base}_{int(time.time())}{ext}"


def make_output_path(archive_dir: str, streamer: str, title: str = "", when: float | None = None) -> str:
    clips_dir = os.path.join(archive_dir, CLIP_SUBDIR)
    os.makedirs(clips_dir, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(when or time.time()))
    streamer_part = sanitize_label(streamer, 32) or "clip"
    title_part = sanitize_label(title, 40)
    name = f"{streamer_part}_{stamp}_Clip.mp4"
    if title_part:
        name = f"{streamer_part}_{stamp}_{title_part}_Clip.mp4"
    return unique_path(os.path.join(clips_dir, name))


def _libx264_encoder() -> dict:
    return {
        "name": "libx264",
        "fast": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26"],
        "high": ["-c:v", "libx264", "-preset", "faster", "-crf", "20"],
    }


def reset_encoder_cache() -> None:
    """Clear the encoder choice so the next clip can detect NVENC/QSV."""
    global _ENCODER_CACHE
    _ENCODER_CACHE = None


def _detect_encoder() -> dict:
    fallback = _libx264_encoder()
    if not ffmpeg_bin():
        return fallback
    # Prefer software encode for clips: NVENC/QSV often appear in `-encoders`
    # without a usable device, which would poison the cache after first failure.
    prefer_hw = str(os.environ.get("CLIP_PREFER_HW_ENCODER", "")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if not prefer_hw:
        return fallback
    try:
        result = subprocess.run(
            [_ffmpeg_cmd(), "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
            text=True,
        )
        listing = result.stdout or ""
    except Exception:
        return fallback
    if "h264_nvenc" in listing:
        return {
            "name": "h264_nvenc",
            "fast": ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "26", "-b:v", "0"],
            "high": ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "20", "-b:v", "0"],
        }
    if "h264_qsv" in listing:
        return {
            "name": "h264_qsv",
            "fast": ["-c:v", "h264_qsv", "-global_quality", "26"],
            "high": ["-c:v", "h264_qsv", "-global_quality", "20"],
        }
    return fallback


def video_encoder_args(quality: str = "fast") -> list[str]:
    global _ENCODER_CACHE
    if _ENCODER_CACHE is None:
        _ENCODER_CACHE = _detect_encoder()
    key = "high" if quality == "high" else "fast"
    return list(_ENCODER_CACHE.get(key) or _ENCODER_CACHE["fast"])


def audio_encoder_args(include_audio: bool = True) -> list[str]:
    if not include_audio:
        return ["-an"]
    # AAC-LC @ 48 kHz matches livebot HLS; stereo keeps Safari/Discord happy.
    return ["-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000"]


def _force_software_encoder() -> None:
    """Pin subsequent encodes to libx264 after a hardware encoder failure."""
    global _ENCODER_CACHE
    _ENCODER_CACHE = _libx264_encoder()


_LAST_EXTRACT_ERROR = ""


def last_extract_error() -> str:
    return _LAST_EXTRACT_ERROR


def _set_extract_error(message: str) -> None:
    global _LAST_EXTRACT_ERROR
    _LAST_EXTRACT_ERROR = str(message or "").strip()


def _is_http_url(path: str) -> bool:
    text = str(path or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://")


def _input_reachable(path: str) -> bool:
    if not path:
        return False
    if _is_http_url(path):
        return True
    return os.path.isfile(path)


def _ffmpeg_header_args(http_headers: dict | None = None) -> list[str]:
    if not http_headers:
        return []
    lines = []
    for key, value in http_headers.items():
        name = str(key or "").strip()
        if not name:
            continue
        lines.append(f"{name}: {str(value or '').strip()}")
    if not lines:
        return []
    # FFmpeg expects CRLF-separated header lines ending with a blank line.
    payload = "\r\n".join(lines) + "\r\n"
    return ["-headers", payload]


def _concat_file_entry(path: str) -> str:
    """Escape a local path for the FFmpeg concat demuxer (`file '...'` lines)."""
    safe = str(path or "").replace("'", "'\\''")
    return f"file '{safe}'"


def probe_duration_sec(path: str, http_headers: dict | None = None) -> float:
    if not _input_reachable(path):
        return 0.0
    if not ffprobe_bin():
        # Still try PATH name so tests can stub the binary.
        pass
    try:
        cmd = [
            _ffprobe_cmd(),
            "-v",
            "error",
            *_ffmpeg_header_args(http_headers),
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ]
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60 if _is_http_url(path) else 20,
            check=False,
            text=True,
        )
        return float((result.stdout or "").strip() or 0.0)
    except Exception:
        return 0.0


def parse_hls_segments(playlist_path: str) -> list[tuple[str, float]]:
    segments: list[tuple[str, float]] = []
    last_duration = 0.0
    try:
        with open(playlist_path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if line.startswith("#EXTINF:"):
                    try:
                        last_duration = float(line.split(":", 1)[1].split(",", 1)[0])
                    except Exception:
                        last_duration = 0.0
                elif line and not line.startswith("#"):
                    segments.append((line, last_duration))
    except OSError:
        return []
    return segments


def playlist_window_seconds(playlist_path: str) -> float:
    return sum(duration for _name, duration in parse_hls_segments(playlist_path))


def _resolve_segment_path(playlist_dir: str, name: str) -> str:
    """Resolve a playlist URI to a local file; reject absolute URLs and traversal."""
    text = str(name or "").strip()
    if not text or _is_http_url(text):
        return ""
    # Strip optional query fragments from relative URIs.
    text = text.split("?", 1)[0].split("#", 1)[0]
    if os.path.isabs(text):
        path = text
    else:
        path = os.path.normpath(os.path.join(playlist_dir, text))
    base = os.path.abspath(playlist_dir)
    resolved = os.path.abspath(path)
    if resolved != base and not resolved.startswith(base + os.sep):
        return ""
    return resolved if os.path.isfile(resolved) else ""


def _stage_hls_segments(playlist_dir: str, selected: list[str]) -> tuple[str, list[str], float]:
    """Copy selected segments out of the live sliding window before FFmpeg runs.

    livebot uses ``delete_segments``; reading in-place races with the writer.
    Returns (staging_dir, staged_basenames, available_duration_hint).
    """
    staging_dir = tempfile.mkdtemp(prefix="clip_hls_")
    staged: list[str] = []
    try:
        for index, name in enumerate(selected):
            src = _resolve_segment_path(playlist_dir, name)
            if not src:
                logger.warning("HLS clip: missing or unsafe segment %r", name)
                continue
            dest_name = f"seg_{index:05d}{os.path.splitext(src)[1] or '.ts'}"
            dest = os.path.join(staging_dir, dest_name)
            try:
                shutil.copy2(src, dest)
            except OSError:
                logger.warning("HLS clip: could not stage segment %s", src)
                continue
            if os.path.isfile(dest) and os.path.getsize(dest) > 32:
                staged.append(dest_name)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    return staging_dir, staged, 0.0


def _duration_too_long(actual: float, expected: float) -> bool:
    if actual <= 0:
        return False
    return actual > max(expected * 1.5, expected + 5.0)


def _duration_plausible_for_copy(actual: float, expected: float) -> bool:
    """Stream-copy clips may start on the prior keyframe; allow modest overrun."""
    if actual <= 0:
        return True
    if _duration_too_long(actual, expected):
        return False
    # Too short usually means the copy landed mid-GOP / truncated — re-encode.
    if actual < max(0.4, expected * 0.55):
        return False
    return True


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


def _run_ffmpeg(cmd: list[str], timeout: int = FFMPEG_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    """Run FFmpeg with stderr captured; terminate on timeout.

    Uses ``subprocess.run`` so unit tests can patch it. On timeout Python kills
    the child; we additionally try process-group kill when a PID is available.
    """
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            text=True,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("FFmpeg timeout after %ss: %s", timeout, " ".join(cmd[:8]))
        # Best-effort: kill leftover process group if the runtime left a PID.
        proc = getattr(exc, "process", None)
        if proc is not None:
            _kill_process_tree(proc)
        stderr = ""
        if exc.stderr:
            stderr = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else str(exc.stderr)
            )
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr=stderr or "timeout")
    except OSError as exc:
        logger.warning("FFmpeg spawn failed: %s", exc)
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr=str(exc))


def _output_ok(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 1024
    except OSError:
        return False


def _cleanup_failed(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _cleanup_dir(path: str) -> None:
    if not path:
        return
    shutil.rmtree(path, ignore_errors=True)


def _clip_reencode_tail(
    quality: str,
    include_audio: bool,
) -> list[str]:
    """Shared encode args: reset PTS, force IDR at t=0, AAC/H.264, faststart."""
    args = [
        "-vf",
        "setpts=PTS-STARTPTS",
        "-force_key_frames",
        "expr:eq(n,0)",
    ]
    if include_audio:
        args += ["-af", "asetpts=PTS-STARTPTS", "-shortest"]
    args += [
        *video_encoder_args(quality),
        *audio_encoder_args(include_audio),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
    ]
    return args


def _run_encode_with_fallback(
    build_cmd,
    output_path: str,
    quality: str,
    timeout: int = FFMPEG_TIMEOUT_SEC,
) -> subprocess.CompletedProcess:
    """Run an encode; if HW encoder fails, pin to libx264 and retry once."""
    cmd = build_cmd(quality)
    result = _run_ffmpeg(cmd, timeout=timeout)
    if _output_ok(output_path):
        return result
    _cleanup_failed(output_path)
    encoder_name = (_ENCODER_CACHE or {}).get("name") or ""
    if encoder_name and encoder_name != "libx264":
        logger.warning("Clip HW encoder %s failed; falling back to libx264", encoder_name)
        _force_software_encoder()
        cmd = build_cmd(quality)
        result = _run_ffmpeg(cmd, timeout=timeout)
    return result


def concat_hls_range(
    playlist_path: str,
    output_path: str,
    start_sec: float | None,
    duration_sec: float,
    quality: str = "fast",
) -> bool:
    """Cut from a live HLS window. start_sec=None takes the newest tail."""
    duration_sec = max(1.0, min(float(duration_sec), float(MAX_DURATION_SEC)))
    segments = parse_hls_segments(playlist_path)
    if not segments:
        return False

    selected: list[str] = []
    selected_duration = 0.0
    range_start = 0.0
    if start_sec is None:
        for name, duration in reversed(segments):
            selected.append(name)
            selected_duration += duration
            if selected_duration >= duration_sec:
                break
        selected.reverse()
        start_offset = max(0.0, selected_duration - duration_sec)
    else:
        start_sec = max(0.0, float(start_sec))
        end_sec = start_sec + duration_sec
        cursor = 0.0
        first = True
        for name, duration in segments:
            seg_end = cursor + duration
            if seg_end > start_sec and cursor < end_sec:
                if first:
                    range_start = cursor
                    first = False
                selected.append(name)
                selected_duration += duration
            cursor = seg_end
        start_offset = max(0.0, start_sec - range_start)

    if not selected:
        return False

    ok_space, space_err = check_disk_space(os.path.dirname(output_path) or ".")
    if not ok_space:
        logger.warning("HLS clip aborted: %s", space_err)
        return False

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    list_path = ""
    staging_dir = ""
    settings = load_settings()
    quality = quality or settings["quality"]
    try:
        playlist_dir = os.path.dirname(playlist_path) or "."
        staging_dir, staged, _hint = _stage_hls_segments(playlist_dir, selected)
        if not staged:
            logger.warning("HLS clip: no segments could be staged from %s", playlist_path)
            return False

        # Rebuild start_offset if some segments were missing (shorten from the head for tails).
        if len(staged) < len(selected) and start_sec is None:
            start_offset = 0.0

        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", dir=staging_dir) as handle:
            list_path = handle.name
            for name in staged:
                handle.write(_concat_file_entry(os.path.join(staging_dir, name)) + "\n")

        def build_cmd(_quality: str) -> list[str]:
            cmd = [
                _ffmpeg_cmd(),
                "-y",
                "-hide_banner",
                "-nostdin",
                "-fflags",
                "+genpts+igndts+discardcorrupt",
                "-avoid_negative_ts",
                "make_zero",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_path,
            ]
            # Output seek after concat so we decode from a clean timeline.
            if start_offset > 0.05:
                cmd += ["-ss", f"{start_offset:.2f}"]
            cmd += [
                "-t",
                f"{duration_sec:.2f}",
                *_clip_reencode_tail(_quality, settings["include_audio"]),
                output_path,
            ]
            return cmd

        result = _run_encode_with_fallback(build_cmd, output_path, quality)
        if not _output_ok(output_path):
            err = (result.stderr or "").strip().splitlines()
            logger.warning(
                "HLS clip concat failed (rc=%s): %s",
                result.returncode,
                err[-1] if err else "no output",
            )
            _cleanup_failed(output_path)
            return False
        actual = probe_duration_sec(output_path)
        if _duration_too_long(actual, duration_sec):
            logger.warning(
                "HLS clip duration mismatch: got %.1fs expected ~%.1fs",
                actual,
                duration_sec,
            )
            _cleanup_failed(output_path)
            return False
        return True
    except Exception:
        logger.exception("HLS clip concat failed")
        _cleanup_failed(output_path)
        return False
    finally:
        if list_path:
            try:
                os.remove(list_path)
            except OSError:
                pass
        _cleanup_dir(staging_dir)


def concat_hls_tail(playlist_path: str, output_path: str, duration_sec: float, quality: str = "fast") -> bool:
    """Last N seconds of a live HLS playlist. Alias used by chat clips."""
    return concat_hls_range(playlist_path, output_path, None, duration_sec, quality=quality)


def _recording_meta_paths(archive_dir: str) -> list[str]:
    paths: list[str] = []
    try:
        for name in os.listdir(archive_dir):
            if name == ".current_recording.json" or name.startswith(".current_recording."):
                if name.endswith(".json"):
                    paths.append(os.path.join(archive_dir, name))
    except OSError:
        return []
    # Prefer source-specific markers before the generic file.
    paths.sort(key=lambda path: (0 if ".current_recording." in os.path.basename(path) else 1, path))
    return paths


def estimate_recording_duration(input_path: str) -> float:
    """Best-effort media length for finished or still-growing recordings.

    Growing ``.mkv.part`` / ``.mp4.part`` files often report duration N/A to
    ffprobe. Fall back to recorder ``start_ts`` from ``.current_recording*.json``.
    """
    if not input_path or not os.path.isfile(input_path):
        return 0.0
    probed = probe_duration_sec(input_path)
    if probed > 1.0:
        return probed
    archive_dir = os.path.dirname(input_path) or "."
    base = os.path.basename(input_path)
    stem = base.removesuffix(".part")
    stem_root, _ext = os.path.splitext(stem)
    aliases = {base.lower(), stem.lower(), stem_root.lower()}
    for meta_path in _recording_meta_paths(archive_dir):
        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                data = json.load(handle) or {}
        except Exception:
            continue
        names = []
        for key in ("record_file", "filename"):
            value = str(data.get(key) or "").strip()
            if value and os.path.basename(value) == value:
                names.append(value)
                root, _ = os.path.splitext(value)
                if root:
                    names.append(root)
        if not names:
            continue
        name_set = {n.lower() for n in names}
        if aliases.isdisjoint(name_set) and stem.lower() not in name_set and stem_root.lower() not in name_set:
            # Still allow match when this is the only active recording in the folder.
            if len(_recording_meta_paths(archive_dir)) > 1:
                continue
        try:
            start_ts = float(data.get("start_ts") or 0.0)
        except (TypeError, ValueError):
            start_ts = 0.0
        if start_ts <= 0:
            continue
        elapsed = time.time() - start_ts
        if elapsed > 1.0:
            return elapsed
    return 0.0


def extract_recording_tail(
    input_path: str,
    output_path: str,
    duration_sec: float,
    quality: str = "fast",
) -> bool:
    if not input_path or not os.path.isfile(input_path):
        return False
    duration_sec = max(1.0, min(float(duration_sec), float(MAX_DURATION_SEC)))
    ok_space, space_err = check_disk_space(os.path.dirname(output_path) or ".")
    if not ok_space:
        logger.warning("Recording tail aborted: %s", space_err)
        return False
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    settings = load_settings()
    quality = quality or settings["quality"]

    def build_sseof(_quality: str) -> list[str]:
        return [
            _ffmpeg_cmd(),
            "-y",
            "-hide_banner",
            "-nostdin",
            "-fflags",
            "+genpts+igndts+discardcorrupt",
            "-avoid_negative_ts",
            "make_zero",
            "-sseof",
            f"-{duration_sec:.2f}",
            "-i",
            input_path,
            "-t",
            f"{duration_sec:.2f}",
            *_clip_reencode_tail(_quality, settings["include_audio"]),
            output_path,
        ]

    def build_seek(start: float, _quality: str) -> list[str]:
        return [
            _ffmpeg_cmd(),
            "-y",
            "-hide_banner",
            "-nostdin",
            "-fflags",
            "+genpts+igndts+discardcorrupt",
            "-avoid_negative_ts",
            "make_zero",
            "-ss",
            f"{start:.3f}",
            "-i",
            input_path,
            "-t",
            f"{duration_sec:.2f}",
            *_clip_reencode_tail(_quality, settings["include_audio"]),
            output_path,
        ]

    try:
        # Never use -sseof when container duration is unknown: FFmpeg ignores it and
        # copies from the start of the growing .part file (looks like "clip from VOD start").
        total = estimate_recording_duration(input_path)
        probed = probe_duration_sec(input_path)
        if probed > 1.0:
            result = _run_encode_with_fallback(build_sseof, output_path, quality)
            if _output_ok(output_path):
                return True
            _cleanup_failed(output_path)
            total = max(total, probed)
        if total <= 1.0:
            logger.warning(
                "Recording tail extract failed: unknown duration for %s",
                os.path.basename(input_path),
            )
            return False
        start = max(0.0, total - duration_sec)
        result = _run_encode_with_fallback(
            lambda q: build_seek(start, q),
            output_path,
            quality,
        )
        if _output_ok(output_path):
            return True
        _cleanup_failed(output_path)
        logger.warning(
            "Recording tail extract failed (rc=%s) start=%.1fs",
            getattr(result, "returncode", "?"),
            start,
        )
        return False
    except Exception:
        logger.exception("Recording tail extract failed")
        _cleanup_failed(output_path)
        return False


def extract_vod_range(
    input_path: str,
    output_path: str,
    start_sec: float,
    duration_sec: float,
    quality: str = "fast",
    http_headers: dict | None = None,
) -> bool:
    _set_extract_error("")
    if not _input_reachable(input_path):
        _set_extract_error("Aufnahme nicht verfügbar.")
        return False
    start_sec = max(0.0, float(start_sec))
    duration_sec = max(0.2, min(float(duration_sec), float(MAX_DURATION_SEC)))
    out_dir = os.path.dirname(output_path) or "."
    ok_space, space_err = check_disk_space(out_dir)
    if not ok_space:
        logger.warning("VOD clip aborted: %s", space_err)
        _set_extract_error(space_err or "Nicht genug Speicherplatz für den Clip.")
        return False
    os.makedirs(out_dir, exist_ok=True)
    settings = load_settings()
    quality = quality or settings["quality"]
    remote = _is_http_url(input_path)
    header_args = _ffmpeg_header_args(http_headers if remote else None)
    timeout = FFMPEG_TIMEOUT_SEC if remote else 120

    # Remote: input-seek first (Range). Local copy: input-seek + stream copy, then
    # validate duration; re-encode on keyframe mis-cut / failure.
    seek_prefix = [
        _ffmpeg_cmd(),
        "-y",
        "-hide_banner",
        "-nostdin",
        *header_args,
        "-fflags",
        "+genpts+igndts+discardcorrupt",
        "-ss",
        f"{start_sec:.3f}",
        "-i",
        input_path,
        "-t",
        f"{duration_sec:.3f}",
    ]
    copy_cmd = [
        *seek_prefix,
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        output_path,
    ]
    try:
        _run_ffmpeg(copy_cmd, timeout=timeout)
        if _output_ok(output_path):
            actual = probe_duration_sec(output_path)
            if _duration_plausible_for_copy(actual, duration_sec):
                return True
            logger.info(
                "VOD stream-copy duration implausible (got %.2fs want ~%.2fs); re-encoding",
                actual,
                duration_sec,
            )
            _cleanup_failed(output_path)
        else:
            _cleanup_failed(output_path)

        def build_reencode(_quality: str) -> list[str]:
            # Accurate cut: decode-seek (-ss after -i) for local; keep input-seek for remote speed.
            if remote:
                return [
                    *seek_prefix,
                    *_clip_reencode_tail(_quality, settings["include_audio"]),
                    output_path,
                ]
            return [
                _ffmpeg_cmd(),
                "-y",
                "-hide_banner",
                "-nostdin",
                "-fflags",
                "+genpts+igndts+discardcorrupt",
                "-i",
                input_path,
                "-ss",
                f"{start_sec:.3f}",
                "-t",
                f"{duration_sec:.3f}",
                *_clip_reencode_tail(_quality, settings["include_audio"]),
                output_path,
            ]

        result = _run_encode_with_fallback(build_reencode, output_path, quality, timeout=timeout)
        if _output_ok(output_path):
            actual = probe_duration_sec(output_path)
            if _duration_too_long(actual, duration_sec):
                # Re-encode used ``-t``; keep the file but surface the probe anomaly.
                logger.warning(
                    "VOD clip duration probe unexpected: got %.1fs expected ~%.1fs (keeping output)",
                    actual,
                    duration_sec,
                )
            return True
        err = (result.stderr or "").strip().splitlines()
        detail = err[-1] if err else "no output"
        logger.warning(
            "VOD clip extract failed (rc=%s): %s",
            result.returncode,
            detail,
        )
        _set_extract_error("Clip fehlgeschlagen.")
        _cleanup_failed(output_path)
        return False
    except Exception as exc:
        logger.exception("VOD clip extract failed")
        _set_extract_error(str(exc) or "Clip fehlgeschlagen.")
        _cleanup_failed(output_path)
        return False


def _recording_candidate_paths(archive_dir: str, name: str) -> list[str]:
    """Resolve on-disk recording paths for a metadata basename (.mp4 vs .mkv.part)."""
    name = str(name or "").strip()
    if not name or os.path.basename(name) != name:
        return []
    stem, ext = os.path.splitext(name)
    names = [name]
    # Twitch copy recordings often store filename as .mp4 while writing .mkv.part.
    for alt_ext in (".mkv", ".mp4", ".ts", ".m4s"):
        if ext.lower() == alt_ext:
            continue
        alt = f"{stem}{alt_ext}"
        if alt not in names:
            names.append(alt)
    paths: list[str] = []
    for candidate in names:
        paths.append(os.path.join(archive_dir, f"{candidate}.part"))
        paths.append(os.path.join(archive_dir, candidate))
    return paths


def find_active_recording_file(archive_dir: str, source_channel: str = "") -> str:
    archive_dir = str(archive_dir or "").strip()
    if not archive_dir or not os.path.isdir(archive_dir):
        return ""
    suffix = re.sub(r"[^a-z0-9_]+", "", str(source_channel or "").strip().lower())
    candidates = []
    if suffix:
        candidates.append(os.path.join(archive_dir, f".current_recording.{suffix}.json"))
    candidates.append(os.path.join(archive_dir, ".current_recording.json"))
    for meta_path in candidates:
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                data = json.load(handle) or {}
            names = []
            for key in ("record_file", "filename"):
                value = str(data.get(key) or "").strip()
                if value and os.path.basename(value) == value and value not in names:
                    names.append(value)
            for name in names:
                for path in _recording_candidate_paths(archive_dir, name):
                    if os.path.isfile(path) and os.path.getsize(path) > 100_000:
                        return path
        except Exception:
            continue
    try:
        parts = [
            os.path.join(archive_dir, name)
            for name in os.listdir(archive_dir)
            if name.endswith(".part") and not name.startswith(".")
        ]
        parts = [path for path in parts if os.path.isfile(path) and os.path.getsize(path) > 100_000]
        if parts:
            parts.sort(key=lambda path: os.path.getmtime(path), reverse=True)
            return parts[0]
    except OSError:
        pass
    return ""


def create_live_clip(
    *,
    playlist: str,
    recording: str,
    output_path: str,
    duration_sec: float,
    start_sec: float | None = None,
    quality: str = "fast",
) -> tuple[bool, str]:
    duration_sec = max(1.0, min(float(duration_sec), float(MAX_DURATION_SEC)))
    ok_space, space_err = check_disk_space(os.path.dirname(output_path) or ".")
    if not ok_space:
        logger.warning("Live clip aborted: %s", space_err)
        return False, "disk_space"
    window = 0.0
    if playlist and os.path.isfile(playlist):
        window = playlist_window_seconds(playlist)
        # Accept short sliding windows: better a shorter clip than skipping HLS entirely.
        min_window = 2.0 if start_sec is None else max(2.0, min(duration_sec, 5.0) * 0.5)
        if window >= min_window:
            if concat_hls_range(playlist, output_path, start_sec, duration_sec, quality=quality):
                return True, f"hls({window:.1f}s)"
            logger.warning("HLS clip failed, trying recording fallback")
        else:
            logger.warning("HLS window too short (%.1fs), trying recording", window)
    if recording and os.path.isfile(recording):
        if start_sec is None:
            if extract_recording_tail(recording, output_path, duration_sec, quality=quality):
                return True, f"recording:{os.path.basename(recording)}"
        else:
            # Map HLS-window offset onto the growing recording (approx. wall clock).
            rec_dur = estimate_recording_duration(recording)
            probed = probe_duration_sec(recording)
            if probed > 1.0:
                rec_dur = max(rec_dur, probed)
            if rec_dur > 1.0:
                if window > 0:
                    mapped = max(0.0, rec_dur - window + float(start_sec))
                else:
                    mapped = max(0.0, rec_dur - duration_sec)
                if extract_vod_range(recording, output_path, mapped, duration_sec, quality=quality):
                    return True, f"recording:{os.path.basename(recording)}"
            elif extract_recording_tail(recording, output_path, duration_sec, quality=quality):
                return True, f"recording:{os.path.basename(recording)}"
    return False, "no_source"


def _new_job(kind: str, streamer: str, **extra) -> dict:
    now = time.time()
    job = {
        "id": os.urandom(8).hex(),
        "kind": kind,
        "status": "queued",
        "message": "Clip wird erstellt...",
        "streamer": streamer,
        "clip_filename": None,
        "error": None,
        "created_at": now,
        "trigger_at": extra.pop("trigger_at", now),
    }
    job.update(extra)
    return job


def _jobs_store_dir() -> str:
    path = os.path.join(config.META_DIR, JOBS_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def _job_store_path(job_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9]", "", str(job_id or ""))
    if not safe:
        return ""
    return os.path.join(_jobs_store_dir(), f"{safe}.json")


def _job_public_snapshot(job: dict) -> dict:
    return {
        "id": job.get("id"),
        "kind": job.get("kind"),
        "status": job.get("status"),
        "message": job.get("message") or "",
        "streamer": job.get("streamer"),
        "clip_filename": job.get("clip_filename"),
        "error": job.get("error"),
        "created_at": job.get("created_at"),
        "updated_at": time.time(),
    }


def _atomic_write_json(path: str, data: dict) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".clipjob_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _persist_job(job: dict | None) -> None:
    if not job or not job.get("id"):
        return
    path = _job_store_path(job["id"])
    if not path:
        return
    try:
        _atomic_write_json(path, _job_public_snapshot(job))
    except Exception:
        logger.exception("Could not persist clip job %s", job.get("id"))


def _load_persisted_job(job_id: str) -> dict | None:
    path = _job_store_path(job_id)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not data.get("id"):
            return None
        return data
    except Exception:
        logger.exception("Could not load clip job %s", job_id)
        return None


def _prune_persisted_jobs(now: float | None = None) -> None:
    root = os.path.join(config.META_DIR, JOBS_SUBDIR)
    if not os.path.isdir(root):
        return
    cutoff = (now or time.time()) - JOB_PERSIST_TTL_SEC
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(root, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
        except OSError:
            continue


def public_job(job: dict | None) -> dict:
    if not job:
        return {"ok": False, "status": "error", "message": "Clip-Auftrag nicht gefunden."}
    status = job.get("status")
    return {
        "ok": status != "error",
        "id": job.get("id"),
        "job_id": job.get("id"),
        "status": status,
        "message": job.get("message") or "",
        "clip_filename": job.get("clip_filename"),
        "error": job.get("error"),
        "streamer": job.get("streamer"),
    }


def get_job(job_id: str) -> dict | None:
    key = str(job_id or "")
    with _LOCK:
        job = _JOBS.get(key)
        if job:
            return dict(job)
    return _load_persisted_job(key)


def _ensure_worker() -> None:
    global _WORKER
    if _TEST_SYNC:
        return
    if _WORKER and _WORKER.is_alive():
        return
    _STOP.clear()
    _WORKER = threading.Thread(target=_worker_loop, name="clip-engine", daemon=True)
    _WORKER.start()


def _worker_loop() -> None:
    while not _STOP.is_set():
        job = None
        with _CV:
            while not _QUEUE and not _STOP.is_set():
                _CV.wait(timeout=0.4)
            if _STOP.is_set():
                return
            if _QUEUE:
                job_id = _QUEUE.popleft()
                job = _JOBS.get(job_id)
        if job:
            _run_job(job)


def _wait_until(deadline: float) -> None:
    while not _STOP.is_set():
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.25, remaining))


def _run_job(job: dict) -> None:
    kind = job.get("kind")
    postroll = float(job.get("postroll_sec") or 0)
    if kind == "live_instant" and postroll > 0 and not _TEST_SYNC:
        job["status"] = "waiting"
        job["message"] = "Clip wird erstellt..."
        _persist_job(job)
        _wait_until(float(job.get("trigger_at") or time.time()) + postroll)
        if _STOP.is_set():
            job["status"] = "error"
            job["error"] = "Clip abgebrochen."
            job["message"] = "Clip abgebrochen."
            _persist_job(job)
            return
    job["status"] = "running"
    job["message"] = "Clip wird gespeichert..."
    _persist_job(job)
    ok = False
    source = ""
    with _ENCODE_LOCK:
        try:
            if kind in ("live_instant", "live_range"):
                ok, source = create_live_clip(
                    playlist=job.get("playlist") or "",
                    recording=job.get("recording") or "",
                    output_path=job["output_path"],
                    duration_sec=float(job["duration_sec"]),
                    start_sec=job.get("start_sec"),
                    quality=job.get("quality") or "fast",
                )
            elif kind in ("vod_instant", "vod_range"):
                ok = extract_vod_range(
                    job["input_path"],
                    job["output_path"],
                    float(job.get("start_sec") or 0),
                    float(job["duration_sec"]),
                    quality=job.get("quality") or "fast",
                    http_headers=job.get("http_headers"),
                )
                source = str(job.get("source") or "vod")
        except Exception:
            logger.exception("Clip job %s failed", job.get("id"))
            ok = False
    if ok and _output_ok(job["output_path"]):
        job["status"] = "done"
        job["clip_filename"] = os.path.basename(job["output_path"])
        job["message"] = "Clip gespeichert."
        job["error"] = None
        _persist_job(job)
        logger.info("Clip saved: %s source=%s", job["output_path"], source)
        return
    detail = ""
    if kind in ("vod_instant", "vod_range"):
        detail = last_extract_error()
    elif source == "no_source":
        detail = "Live-Stream nicht verfügbar."
    elif source == "disk_space":
        detail = "Nicht genug Speicherplatz für den Clip."
    job["status"] = "error"
    job["error"] = detail or "Clip fehlgeschlagen."
    job["message"] = detail or "Clip fehlgeschlagen."
    _persist_job(job)
    _cleanup_failed(job.get("output_path") or "")


def _enqueue(job: dict, debounce_key: str = "") -> dict:
    now = time.time()
    with _CV:
        if debounce_key:
            last = _LAST_INSTANT.get(debounce_key)
            if last and now - last[0] < DEBOUNCE_SEC:
                existing = _JOBS.get(last[1])
                if existing:
                    return existing
        if len(_QUEUE) >= MAX_QUEUE:
            job["status"] = "error"
            job["error"] = "Zu viele Clips in der Warteschlange."
            job["message"] = job["error"]
            _JOBS[job["id"]] = job
            _persist_job(job)
            return job
        _JOBS[job["id"]] = job
        _QUEUE.append(job["id"])
        if debounce_key:
            _LAST_INSTANT[debounce_key] = (now, job["id"])
        if len(_JOBS) > 80:
            keep = set(_QUEUE)
            keep.add(job["id"])
            stale = [
                job_id
                for job_id, item in _JOBS.items()
                if job_id not in keep and item.get("status") in ("done", "error")
            ]
            for job_id in stale[:-40]:
                _JOBS.pop(job_id, None)
        _persist_job(job)
        if len(_JOBS) <= 2 or (int(now) % 17 == 0):
            _prune_persisted_jobs(now)
        _ensure_worker()
        _CV.notify()
    if _TEST_SYNC:
        _run_job(job)
    return job


def enqueue_live_instant(
    streamer: str,
    archive_dir: str,
    playlist: str,
    recording: str,
    title: str = "",
    preroll_sec: int | None = None,
    postroll_sec: int | None = None,
) -> dict:
    job = _new_job("live_instant", streamer)
    ok, err = check_ffmpeg()
    if not ok:
        job["status"] = "error"
        job["error"] = err
        job["message"] = err
        return job
    if not archive_dir:
        job["status"] = "error"
        job["error"] = "Clip-Pfad ungültig."
        job["message"] = job["error"]
        return job
    ok, err = check_disk_space(archive_dir)
    if not ok:
        job["status"] = "error"
        job["error"] = err
        job["message"] = err
        return job
    has_playlist = bool(playlist and os.path.isfile(playlist))
    has_recording = bool(recording and os.path.isfile(recording))
    if not has_playlist and not has_recording:
        job["status"] = "error"
        job["error"] = "Aufnahme nicht aktiv."
        job["message"] = job["error"]
        return job
    settings = load_settings()
    # Live instant clips always take the last minute of the active stream/recording.
    if preroll_sec is None and postroll_sec is None:
        preroll = LIVE_INSTANT_DURATION_SEC
        postroll = 0
        duration = LIVE_INSTANT_DURATION_SEC
    else:
        preroll = _clamp_int(
            preroll_sec if preroll_sec is not None else settings["preroll_sec"],
            MIN_PREROLL_SEC,
            MAX_PREROLL_SEC,
            DEFAULT_PREROLL_SEC,
        )
        postroll = _clamp_int(
            postroll_sec if postroll_sec is not None else settings["postroll_sec"],
            MIN_POSTROLL_SEC,
            MAX_POSTROLL_SEC,
            DEFAULT_POSTROLL_SEC,
        )
        duration = min(MAX_DURATION_SEC, preroll + postroll)
    job.update(
        {
            "playlist": playlist,
            "recording": recording,
            "output_path": make_output_path(archive_dir, streamer, title),
            "duration_sec": duration,
            "start_sec": None,
            "preroll_sec": preroll,
            "postroll_sec": 0 if _TEST_SYNC else postroll,
            "quality": settings["quality"],
            "title": title,
        }
    )
    return _enqueue(job, debounce_key=f"live:{streamer}")


def enqueue_live_range(
    streamer: str,
    archive_dir: str,
    playlist: str,
    recording: str,
    start_sec: float | None,
    duration_sec: float,
    title: str = "",
) -> dict:
    """Queue a live trim (Zuschneiden) with HLS first, recording fallback."""
    job = _new_job("live_range", streamer)
    ok, err = check_ffmpeg()
    if not ok:
        job["status"] = "error"
        job["error"] = err
        job["message"] = err
        return job
    if not archive_dir:
        job["status"] = "error"
        job["error"] = "Clip-Pfad ungültig."
        job["message"] = job["error"]
        return job
    ok, err = check_disk_space(archive_dir)
    if not ok:
        job["status"] = "error"
        job["error"] = err
        job["message"] = err
        return job
    has_playlist = bool(playlist and os.path.isfile(playlist))
    has_recording = bool(recording and os.path.isfile(recording))
    if not has_playlist and not has_recording:
        job["status"] = "error"
        job["error"] = "Live-Stream nicht verfügbar."
        job["message"] = job["error"]
        return job
    settings = load_settings()
    duration = max(1.0, min(float(duration_sec or 0.0), float(MAX_DURATION_SEC)))
    start = None if start_sec is None else max(0.0, float(start_sec))
    job.update(
        {
            "playlist": playlist if has_playlist else "",
            "recording": recording if has_recording else "",
            "output_path": make_output_path(archive_dir, streamer, title),
            "duration_sec": duration,
            "start_sec": start,
            "quality": settings["quality"],
            "title": title,
        }
    )
    return _enqueue(job, debounce_key="")


def enqueue_vod_instant(
    streamer: str,
    archive_dir: str,
    input_path: str,
    position_sec: float,
    title: str = "",
    preroll_sec: int | None = None,
    postroll_sec: int | None = None,
    http_headers: dict | None = None,
    source: str = "vod",
) -> dict:
    job = _new_job("vod_instant", streamer)
    ok, err = check_ffmpeg()
    if not ok:
        job["status"] = "error"
        job["error"] = err
        job["message"] = err
        return job
    if not _input_reachable(input_path):
        job["status"] = "error"
        job["error"] = "Aufnahme nicht verfügbar."
        job["message"] = job["error"]
        return job
    ok, err = check_disk_space(archive_dir or ("" if _is_http_url(input_path) else os.path.dirname(input_path)))
    if not ok:
        job["status"] = "error"
        job["error"] = err
        job["message"] = err
        return job
    settings = load_settings()
    preroll = _clamp_int(
        preroll_sec if preroll_sec is not None else settings["preroll_sec"],
        MIN_PREROLL_SEC,
        MAX_PREROLL_SEC,
        DEFAULT_PREROLL_SEC,
    )
    postroll = _clamp_int(
        postroll_sec if postroll_sec is not None else settings["postroll_sec"],
        MIN_POSTROLL_SEC,
        MAX_POSTROLL_SEC,
        DEFAULT_POSTROLL_SEC,
    )
    position = max(0.0, float(position_sec or 0.0))
    media_duration = probe_duration_sec(input_path, http_headers=http_headers)
    start = max(0.0, position - preroll)
    end = position + postroll
    if media_duration > 0:
        end = min(media_duration, end)
    duration = max(1.0, end - start)
    out_base = archive_dir or ("" if _is_http_url(input_path) else os.path.dirname(input_path))
    job.update(
        {
            "input_path": input_path,
            "http_headers": dict(http_headers) if http_headers else None,
            "source": source or "vod",
            "output_path": make_output_path(out_base, streamer, title),
            "start_sec": start,
            "duration_sec": duration,
            "preroll_sec": preroll,
            "postroll_sec": 0,
            "quality": settings["quality"],
            "title": title,
        }
    )
    return _enqueue(job, debounce_key=f"vod:{streamer}")


def enqueue_vod_range(
    streamer: str,
    archive_dir: str,
    input_path: str,
    start_sec: float,
    duration_sec: float,
    title: str = "",
    http_headers: dict | None = None,
    source: str = "vod",
) -> dict:
    """Queue an explicit VOD range cut (editor / overlay)."""
    job = _new_job("vod_range", streamer)
    ok, err = check_ffmpeg()
    if not ok:
        job["status"] = "error"
        job["error"] = err
        job["message"] = err
        return job
    if not _input_reachable(input_path):
        job["status"] = "error"
        job["error"] = "Aufnahme nicht verfügbar."
        job["message"] = job["error"]
        return job
    ok, err = check_disk_space(archive_dir or ("" if _is_http_url(input_path) else os.path.dirname(input_path)))
    if not ok:
        job["status"] = "error"
        job["error"] = err
        job["message"] = err
        return job
    settings = load_settings()
    start = max(0.0, float(start_sec or 0.0))
    duration = max(0.2, min(float(duration_sec or 0.0), float(MAX_DURATION_SEC)))
    out_base = archive_dir or ("" if _is_http_url(input_path) else os.path.dirname(input_path))
    job.update(
        {
            "input_path": input_path,
            "http_headers": dict(http_headers) if http_headers else None,
            "source": source or "vod",
            "output_path": make_output_path(out_base, streamer, title),
            "start_sec": start,
            "duration_sec": duration,
            "quality": settings["quality"],
            "title": title,
        }
    )
    return _enqueue(job, debounce_key="")


def shutdown() -> None:
    _STOP.set()
    with _CV:
        _CV.notify_all()
    acquired = _ENCODE_LOCK.acquire(timeout=20.0)
    if acquired:
        _ENCODE_LOCK.release()
    worker = _WORKER
    if worker and worker.is_alive() and worker is not threading.current_thread():
        worker.join(timeout=5.0)


def reset_for_tests(*, sync: bool = True, sync_mode: bool | None = None, **kwargs) -> None:
    global _TEST_SYNC, _WORKER, _ENCODER_CACHE
    if "sync" in kwargs:
        sync = bool(kwargs["sync"])
    if sync_mode is not None:
        sync = sync_mode
    shutdown()
    with _LOCK:
        _JOBS.clear()
        _QUEUE.clear()
        _LAST_INSTANT.clear()
        _STOP.clear()
        _WORKER = None
        _TEST_SYNC = sync
    try:
        root = os.path.join(config.META_DIR, JOBS_SUBDIR)
        if os.path.isdir(root):
            for name in os.listdir(root):
                if name.endswith(".json") or name.startswith(".clipjob_"):
                    try:
                        os.unlink(os.path.join(root, name))
                    except OSError:
                        pass
    except OSError:
        pass
    _ENCODER_CACHE = _libx264_encoder()


atexit.register(shutdown)
