#!/usr/bin/env python3
"""Live restream daemon: streamlink -> ffmpeg (copy) -> MPEG-TS fan-out + HLS.

Demand driven. The WebUI touches a marker per channel while somebody has the
live page open (``touch_live_heartbeat``); livebot runs one pipeline per marked
channel and stops it when the marker goes stale and no TS viewer is connected.
Without a fresh marker nothing starts, neither from the loop nor over HTTP.

Outputs per channel:

* ``LIVE_HLS_DIR/<channel>/stream.m3u8`` + ``seg_NNNNN.ts`` (served by nginx
  or the WebUI),
* ``GET /live/<channel>.ts`` on LIVEBOT_HOST:LIVEBOT_PORT (behind nginx
  ``auth_request``; this port has no login of its own).

Streamlink's Twitch plugin drops ad segments itself. Twitch only: the live
watch page does not play other platforms.

Run from the repository root: ``python -m recorder.livebot``
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import queue
import random
import re
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from core import config
from core import database
from core import twitch_api
from recorder.recording_runtime import SubprocessRunner
from recorder.ts_fanout import MpegTsBroadcaster

logger = logging.getLogger("livebot")

CHANNEL_RE = re.compile(r"[a-z0-9_]{1,25}")
TS_PATH_RE = re.compile(r"/live/([a-z0-9_]{1,25})\.ts")
# Only these names are ever deleted below LIVE_HLS_DIR.
HLS_FILE_RE = re.compile(r"(?:seg_\d+\.ts|stream\.m3u8|index\.m3u8)(?:\.tmp)?|streamlink\.log|ffmpeg\.log")

POLL_INTERVAL_SEC = 1.0
MAINTENANCE_INTERVAL_SEC = 30.0
FIRST_BYTE_TIMEOUT_SEC = 25.0
CLIENT_TIMEOUT_SEC = 30
STOP_TIMEOUT_SEC = 5
# After this many failed starts in a row a configured playlist proxy is skipped.
PROXY_DIRECT_AFTER_FAILS = 2
# At the channel limit an unwatched pipeline may be replaced once its marker is
# older than a throttled background tab (60 s) would let it get.
EVICT_IDLE_SEC = 70
PIPELINE_LOG_MAX_BYTES = 1024 * 1024
# ffmpeg needs one complete AAC frame inside the probe window: with 32 KB (about
# 40 ms at 1080p60) 3 of 10 cold starts ended with 0 bytes of output.
FFMPEG_PROBESIZE = 524288
FFMPEG_ANALYZEDURATION_US = 1000000

_LOG_THROTTLE: dict[str, float] = {}


def throttled_log(level, key, message, *args, interval=300):
    now = time.time()
    if now - _LOG_THROTTLE.get(key, 0.0) < interval:
        return
    _LOG_THROTTLE[key] = now
    logger.log(level, message, *args)


# --- demand markers and HLS directory ----------------------------------------

def valid_channel(name):
    return isinstance(name, str) and CHANNEL_RE.fullmatch(name) is not None


def hls_dir_for(channel):
    if not valid_channel(channel):
        raise ValueError("invalid channel name")
    return os.path.join(config.LIVE_HLS_ROOT, channel)


def demand_dir():
    return os.path.join(config.META_DIR, "live_watch_demand")


def marker_paths(channel):
    """The two files the WebUI touches while somebody watches."""
    return (
        os.path.join(hls_dir_for(channel), ".heartbeat"),
        os.path.join(demand_dir(), f"{channel}.ts"),
    )


def demand_age(channel):
    """Seconds since the WebUI last reported a viewer (inf = never)."""
    if not valid_channel(channel):
        return float("inf")
    newest = None
    for path in marker_paths(channel):
        try:
            mtime = os.path.getmtime(path)
        except OSError:  # no marker
            continue
        newest = mtime if newest is None else max(newest, mtime)
    return float("inf") if newest is None else max(0.0, time.time() - newest)


def has_demand(channel):
    return demand_age(channel) <= config.LIVE_WATCH_DEMAND_SEC


def _listdir(path):
    try:
        return os.listdir(path)
    except FileNotFoundError:
        return []
    except OSError as exc:
        throttled_log(logging.WARNING, f"listdir-{path}", "Cannot list %s: %s", path, exc, interval=60)
        return []


def _known_channels():
    """Every channel with a marker or an HLS directory."""
    names = {name[:-3] for name in _listdir(demand_dir()) if name.endswith(".ts")}
    names.update(_listdir(config.LIVE_HLS_ROOT))
    return {name for name in names if valid_channel(name)}


def demanded_channels():
    """Channels with a fresh marker, most recently requested first."""
    ages = {name: demand_age(name) for name in _known_channels()}
    fresh = [name for name, age in ages.items() if age <= config.LIVE_WATCH_DEMAND_SEC]
    return sorted(fresh, key=lambda name: (ages[name], name))


def _remove_file(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Cannot remove %s: %s", path, exc)


def clear_channel_files(channel):
    """Remove the pipeline files of one channel; markers of a waiting viewer stay.

    Stale markers go as well, and then the empty directory.
    """
    directory = hls_dir_for(channel)
    if not has_demand(channel):
        for path in marker_paths(channel):
            _remove_file(path)
    if os.path.islink(directory) or not os.path.isdir(directory):
        return
    for name in _listdir(directory):
        if HLS_FILE_RE.fullmatch(name):
            _remove_file(os.path.join(directory, name))
    if not _listdir(directory):
        try:
            os.rmdir(directory)
        except OSError as exc:  # the WebUI may just have written a new marker
            logger.info("HLS directory of %s kept: %s", channel, exc)


def purge_leftovers(active):
    """Remove files and stale markers of channels without pipeline and demand."""
    for name in _known_channels():
        if name not in active and not has_demand(name):
            clear_channel_files(name)


def hls_space_error():
    """Empty string, or why there is not enough room for another pipeline."""
    need = config.LIVE_HLS_MIN_FREE_MB
    try:
        os.makedirs(config.LIVE_HLS_ROOT, exist_ok=True)
        free = shutil.disk_usage(config.LIVE_HLS_ROOT).free // (1024 * 1024)
    except OSError as exc:
        return f"HLS directory not usable: {exc}"
    return "" if free >= need else f"only {free} MB free for live HLS, {need} MB required"


# --- channels table -----------------------------------------------------------

@database.db_retry(max_retries=5, delay=0.1, backoff=2.0)
def reset_live_flag(channel):
    """Clear live=1 of an unwatched channel.

    Only rows the watch page created (record=0, chat=0, notify=0) are deleted.
    The flag of a configured live-only channel (record=0, notify=1) belongs to the
    recorder's live monitor and stays untouched.
    """
    with database.db_transaction() as conn:
        conn.execute(
            "UPDATE channels SET live=0 WHERE name=? AND live=1 AND (record=1 OR COALESCE(notify, 1)=0)",
            (channel,),
        )
        conn.execute(
            "DELETE FROM channels WHERE name=? AND record=0 AND chat=0 AND live=0 AND COALESCE(notify, 1)=0",
            (channel,),
        )


def reset_unwatched_flags(active):
    """Clear live=1 of every channel without demand and without a pipeline."""
    try:
        for channel in database.get_channels(mode="live"):
            if channel not in active and not has_demand(channel):
                reset_live_flag(channel)
    except sqlite3.Error as exc:
        logger.error("Live flags not reset: %s", exc)


# --- commands -----------------------------------------------------------------

def _tool(env_name, name):
    candidates = [
        os.environ.get(env_name, "").strip(),
        os.path.join(os.path.dirname(sys.executable), name),
        os.path.join(os.path.dirname(sys.executable), f"{name}.exe"),
        shutil.which(name) or "",
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise FileNotFoundError(f"{name} not found (set {env_name} or install it)")


def proxy_args():
    """Opt-in playlist proxy; nothing is built in.

    Streamlink's own Twitch plugin has no proxy options, so a list without a
    replacement plugin would only make every start fail.
    """
    proxies = os.environ.get("TWITCH_PROXY_PLAYLIST", "").strip()
    if not proxies:
        return []
    plugin_dir = os.environ.get("STREAMLINK_PLUGIN_DIR", "").strip()
    if not plugin_dir:
        throttled_log(
            logging.WARNING, "proxy-without-plugin",
            "TWITCH_PROXY_PLAYLIST needs STREAMLINK_PLUGIN_DIR; connecting to Twitch directly", interval=3600,
        )
        return []
    return ["--plugin-dir", plugin_dir, "--twitch-proxy-playlist", proxies, "--twitch-proxy-playlist-fallback"]


def build_streamlink_command(channel, use_proxy=True):
    """Streamlink writes the ad-filtered stream to stdout.

    The token, if any, is in a private config file: never in argv, and never sent
    through a proxy. The file is livebot's own because the recorder rewrites its
    file with its own token.
    """
    hls_dir_for(channel)  # rejects anything but a plain channel name
    auth = config.streamlink_twitch_config_args(
        config.LIVE_TWITCH_OAUTH_TOKEN, config_dir=os.path.join(config.META_DIR, "livebot")
    )
    cmd = [_tool("STREAMLINK_BIN", "streamlink"), *auth]
    if use_proxy and "--config" not in auth:
        cmd += proxy_args()
    if config.LIVE_TWITCH_LOW_LATENCY:
        cmd.append("--twitch-low-latency")
    return cmd + [
        "--stdout",
        "--loglevel", "info",
        "--retry-open", "3",
        "--stream-timeout", "90",
        "--stream-segment-threads", "3",
        "--default-stream", config.LIVE_STREAM_QUALITY,
        f"https://www.twitch.tv/{channel}",
    ]


def build_ffmpeg_command(hls_dir):
    """Two copies without transcoding: MPEG-TS on stdout and HLS on disk."""
    streams = ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-max_delay", "0"]
    return [
        _tool("FFMPEG_BIN", "ffmpeg"),
        "-hide_banner",
        "-loglevel", "warning",
        "-fflags", "+nobuffer+flush_packets+genpts+igndts",
        "-flags", "low_delay",
        "-probesize", str(FFMPEG_PROBESIZE),
        "-analyzeduration", str(FFMPEG_ANALYZEDURATION_US),
        "-i", "pipe:0",
        *streams,
        "-muxdelay", "0",
        "-muxpreload", "0",
        "-mpegts_flags", "+resend_headers+initial_discontinuity",
        "-f", "mpegts",
        "pipe:1",
        *streams,
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", str(config.LIVEBOT_HLS_LIST_SIZE),
        "-hls_flags", "delete_segments+independent_segments+temp_file",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", os.path.join(hls_dir, "seg_%05d.ts"),
        os.path.join(hls_dir, "stream.m3u8"),
    ]


_ENV_ALLOWED = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TEMP", "TMP",
    "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "VIRTUAL_ENV", "PYTHONUNBUFFERED", "LD_LIBRARY_PATH",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATHEXT", "COMSPEC", "APPDATA", "LOCALAPPDATA", "USERPROFILE",
})


def child_env():
    """Allowlist: application secrets (VOD_PASS, OAuth, storage keys) stay out."""
    return {
        key: value for key, value in os.environ.items()
        if key.upper() in _ENV_ALLOWED or key.upper().startswith("LC_")
    }


# --- processes ----------------------------------------------------------------

def _signal_group(proc, hard):
    """Signal the child's own process group; never ours and never init's."""
    if os.name != "nt":
        try:
            if proc.pid > 1 and proc.pid != os.getpgrp() and os.getpgid(proc.pid) == proc.pid:
                os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
                return
        except ProcessLookupError:
            return
        except OSError as exc:
            logger.warning("Process group %s not signalled: %s", proc.pid, exc)
    try:
        if hard:
            proc.kill()
        else:
            proc.terminate()
    except OSError as exc:  # already gone
        logger.info("Process %s not signalled: %s", proc.pid, exc)


def stop_process(proc, label, timeout=STOP_TIMEOUT_SEC):
    """Terminate, wait with a deadline, then kill. Returns True once reaped."""
    if proc is None or proc.poll() is not None:
        return True
    for hard in (False, True):
        _signal_group(proc, hard)
        try:
            proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            if not hard:
                logger.warning("%s ignores SIGTERM, killing it", label)
    logger.error("%s (pid %s) could not be stopped", label, proc.pid)
    return False


def _open_log(path):
    """Append mode, so a trimmed log stays small; symlinks are refused."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o644), "ab")


class StreamWorker:
    """One channel: streamlink -> ffmpeg -> fan-out + HLS files."""

    def __init__(self, channel, source_command=build_streamlink_command, runner=None):
        self.channel = channel
        self.hls_dir = hls_dir_for(channel)
        self.playlist_file = os.path.join(self.hls_dir, "stream.m3u8")
        self.source_command = source_command
        self.runner = runner or SubprocessRunner()
        self.process_streamlink = None
        self.process_ffmpeg = None
        self.broadcaster = MpegTsBroadcaster(channel)
        self.start_time = time.time()
        self.was_ready = False

    def start(self, use_proxy=True):
        logger.info("Starting live pipeline for %s", self.channel)
        space_error = hls_space_error()
        if space_error:
            logger.error("Live pipeline for %s not started: %s", self.channel, space_error)
            return False
        self.start_time = time.time()
        try:
            if os.path.islink(self.hls_dir):
                raise OSError("HLS directory is a symlink")
            # Segments of the previous run would show up as a frozen picture.
            clear_channel_files(self.channel)
            os.makedirs(self.hls_dir, exist_ok=True)
            self._spawn(self.source_command(self.channel, use_proxy), build_ffmpeg_command(self.hls_dir))
        except (OSError, ValueError) as exc:
            logger.error("Live pipeline for %s not started: %s", self.channel, exc)
            self.stop()
            return False
        self._link_index()
        logger.info(
            "Live pipeline for %s runs (source pid %s, ffmpeg pid %s)",
            self.channel, self.process_streamlink.pid, self.process_ffmpeg.pid,
        )
        return True

    def _spawn(self, source_cmd, ffmpeg_cmd):
        env = child_env()
        with _open_log(os.path.join(self.hls_dir, "streamlink.log")) as source_log:
            self.process_streamlink = self.runner.popen(
                source_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=source_log, env=env
            )
        # Only ffmpeg may hold the read end, otherwise the source never sees it die.
        with self.process_streamlink.stdout as source_out, \
                _open_log(os.path.join(self.hls_dir, "ffmpeg.log")) as ffmpeg_log:
            self.process_ffmpeg = self.runner.popen(
                ffmpeg_cmd, stdin=source_out, stdout=subprocess.PIPE, stderr=ffmpeg_log, env=env
            )
        self.broadcaster.start(self.process_ffmpeg.stdout)

    def _link_index(self):
        """index.m3u8 alias used by live clips and diagnostics."""
        if os.name == "nt":
            return
        try:
            os.symlink("stream.m3u8", os.path.join(self.hls_dir, "index.m3u8"))
        except OSError as exc:
            logger.warning("index.m3u8 alias for %s not created: %s", self.channel, exc)

    def is_alive(self):
        return all(
            proc is not None and proc.poll() is None
            for proc in (self.process_streamlink, self.process_ffmpeg)
        )

    def viewers(self):
        return self.broadcaster.active_viewers()

    def playlist_ready(self):
        return os.path.isfile(self.playlist_file)

    def playlist_age(self):
        try:
            return time.time() - os.path.getmtime(self.playlist_file)
        except OSError:  # not written yet
            return float("inf")

    def failure(self, now):
        """Why the pipeline must be restarted, or an empty string."""
        limit = config.LIVE_STALL_RESTART_SEC
        if not self.is_alive():
            return "process ended"
        if not self.playlist_ready():
            return "no playlist" if now - self.start_time > limit else ""
        if self.playlist_age() > limit:
            return "playlist stale"
        last = self.broadcaster.last_chunk_time
        return "TS output stalled" if last and now - last > limit else ""

    def log_tail(self, name, lines=3, max_chars=400):
        """Last non-empty lines of a pipeline log; read before the next start clears it."""
        try:
            with open(os.path.join(self.hls_dir, name), encoding="utf-8", errors="replace") as handle:
                tail = [line.strip() for line in handle if line.strip()][-lines:]
        except OSError:  # never written
            return "-"
        return (" | ".join(tail) or "-")[-max_chars:]

    def trim_logs(self):
        for name in ("streamlink.log", "ffmpeg.log"):
            path = os.path.join(self.hls_dir, name)
            try:
                if os.path.getsize(path) > PIPELINE_LOG_MAX_BYTES:
                    os.truncate(path, 0)
            except OSError as exc:
                throttled_log(logging.WARNING, f"trim-{path}", "Log %s not trimmed: %s", path, exc)

    def stop(self):
        """Stop the children and clear the files; a waiting viewer keeps its marker.

        Without the marker the next retry would look unwatched and drop its
        backoff, and the page's next poll would restart the channel at once.
        """
        logger.info("Stopping live pipeline for %s", self.channel)
        self.broadcaster.stop()
        # Consumer first, so the source sees a closed pipe.
        stop_process(self.process_ffmpeg, f"ffmpeg {self.channel}")
        stop_process(self.process_streamlink, f"streamlink {self.channel}")
        reader = self.broadcaster.thread
        if reader is not None:
            reader.join(timeout=STOP_TIMEOUT_SEC)
        self.process_ffmpeg = self.process_streamlink = None
        clear_channel_files(self.channel)


# --- daemon -------------------------------------------------------------------

class LiveBot:
    def __init__(self, source_command=build_streamlink_command, is_live=twitch_api.gql_stream_is_live):
        self.source_command = source_command
        self.is_live = is_live
        self.lock = threading.RLock()
        self.streams: dict[str, StreamWorker] = {}
        self.stopping: set[str] = set()
        # channel -> {"fails": int, "next": float, "offline": bool}
        self.retry: dict[str, dict] = {}
        self.stop_event = threading.Event()
        self._last_maintenance = 0.0

    # Starting -------------------------------------------------------------

    def get_or_start(self, channel):
        """Running worker for an HTTP client. Returns (worker, http_status)."""
        if not has_demand(channel):
            return None, 404
        with self.lock:
            worker = self.streams.get(channel)
            if worker is not None and worker.is_alive():
                return worker, 200
            if worker is not None or self.retry.get(channel, {}).get("offline"):
                # Dead worker or offline channel: the loop decides when to try again.
                return None, 503
            worker = self._try_start(channel)
        return (worker, 200) if worker is not None else (None, 503)

    def _try_start(self, channel):
        with self.lock:
            if self.stop_event.is_set() or channel in self.streams or channel in self.stopping:
                return None
            state = self.retry.get(channel, {})
            if state.get("next", 0.0) > time.time():
                return None
            if len(self.streams) + len(self.stopping) >= config.LIVEBOT_MAX_CHANNELS:
                throttled_log(
                    logging.WARNING, "channel-limit",
                    "Channel limit %s reached, %s has to wait", config.LIVEBOT_MAX_CHANNELS, channel, interval=60,
                )
                return None
            worker = StreamWorker(channel, self.source_command)
            if worker.start(use_proxy=state.get("fails", 0) < PROXY_DIRECT_AFTER_FAILS):
                self.streams[channel] = worker
                return worker
            self._schedule_retry(channel, "start failed", live=None)
            return None

    def _schedule_retry(self, channel, reason, live):
        with self.lock:
            fails = self.retry.get(channel, {}).get("fails", 0) + 1
            delay = min(config.LIVE_RETRY_BACKOFF_CAP_SEC, 2 ** min(fails, 6))
            if live is False:
                delay = max(delay, config.LIVE_OFFLINE_RECHECK_SEC)
            delay += random.uniform(0, max(0.2, delay * 0.1))
            self.retry[channel] = {"fails": fails, "next": time.time() + delay, "offline": live is False}
        logger.warning(
            "Live pipeline %s: %s; attempt %s failed, next in %.0fs (twitch_live=%s)",
            channel, reason, fails, delay, live,
        )

    def _still_offline(self, channel):
        """Ask Twitch before starting a channel that was offline the last time."""
        state = self.retry.get(channel, {})
        if not state.get("offline"):
            return False
        if self.is_live(channel) is False:
            state["next"] = time.time() + config.LIVE_OFFLINE_RECHECK_SEC
            throttled_log(logging.INFO, f"offline-{channel}", "Channel %s is still offline", channel, interval=600)
            return True
        state["offline"] = False
        return False

    # Stopping -------------------------------------------------------------

    def _stop_channel(self, channel):
        with self.lock:
            worker = self.streams.pop(channel, None)
            if worker is None:
                return
            self.stopping.add(channel)
        try:
            worker.stop()
        finally:
            with self.lock:
                self.stopping.discard(channel)

    def _evict_idle(self):
        """Free a slot: the unwatched pipeline whose marker is the oldest."""
        with self.lock:
            idle = [(demand_age(name), name) for name, worker in self.streams.items() if not worker.viewers()]
        if idle and max(idle)[0] > EVICT_IDLE_SEC:
            self._stop_channel(max(idle)[1])

    def shutdown(self):
        self.stop_event.set()
        with self.lock:
            channels = list(self.streams)
        if channels:
            with ThreadPoolExecutor(max_workers=len(channels)) as pool:
                list(pool.map(self._stop_channel, channels))

    # Loop -----------------------------------------------------------------

    def tick(self):
        now = time.time()
        # Snapshot first: a pipeline an HTTP client starts after it has a newer marker
        # than the list below and must not look unwatched.
        with self.lock:
            running = dict(self.streams)
        desired = demanded_channels()
        for channel, worker in running.items():
            if channel not in desired and not worker.viewers():
                logger.info("Nobody watches %s any more", channel)
                self._stop_channel(channel)
                self.retry.pop(channel, None)
            else:
                self._check(worker, now)
        for channel in desired:
            if channel not in running:
                self._start_desired(channel, now)
        if now - self._last_maintenance >= MAINTENANCE_INTERVAL_SEC:
            self._last_maintenance = now
            self._maintain(desired)

    def _start_desired(self, channel, now):
        if self.retry.get(channel, {}).get("next", 0.0) > now or self._still_offline(channel):
            return
        if len(self.streams) >= config.LIVEBOT_MAX_CHANNELS:
            self._evict_idle()
        self._try_start(channel)

    def _check(self, worker, now):
        reason = worker.failure(now)
        if reason:
            logger.warning(
                "Live pipeline %s: %s | ffmpeg: %s | streamlink: %s",
                worker.channel, reason, worker.log_tail("ffmpeg.log"), worker.log_tail("streamlink.log"),
            )
            self._stop_channel(worker.channel)
            if has_demand(worker.channel):
                self._schedule_retry(worker.channel, reason, self.is_live(worker.channel))
        elif not worker.was_ready and worker.playlist_ready():
            worker.was_ready = True
            self.retry.pop(worker.channel, None)

    def _maintain(self, desired):
        with self.lock:
            active = set(self.streams) | self.stopping
            for worker in self.streams.values():
                worker.trim_logs()
            for channel in [name for name in self.retry if name not in active and name not in desired]:
                del self.retry[channel]
        cutoff = time.time() - 3600
        for key in [key for key, stamp in list(_LOG_THROTTLE.items()) if stamp < cutoff]:
            _LOG_THROTTLE.pop(key, None)
        purge_leftovers(active)
        reset_unwatched_flags(active)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except (OSError, sqlite3.Error, ValueError, RuntimeError):
                logger.exception("Live loop failed, continuing")
            self.stop_event.wait(POLL_INTERVAL_SEC)


# --- HTTP ---------------------------------------------------------------------

class LiveHttpHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "livebot"
    sys_version = ""
    timeout = CLIENT_TIMEOUT_SEC  # slow requests and viewers that stop reading

    def log_message(self, format, *args):
        """No access log: requests carry the session cookie."""

    def do_HEAD(self):
        self._handle(head_only=True)

    def do_GET(self):
        self._handle(head_only=False)

    def _reply(self, status, body=b"", content_type="text/plain; charset=utf-8"):
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _handle(self, head_only):
        bot = self.server.bot
        path = self.path.split("?", 1)[0]
        if path == "/health":
            body = json.dumps({"status": "ok", "streams": len(bot.streams)}).encode()
            self._reply(200, body, "application/json")
            return
        match = TS_PATH_RE.fullmatch(path)
        if not match:
            self._reply(404)
            return
        channel = match.group(1)
        if head_only:  # never starts a pipeline
            worker = bot.streams.get(channel)
            self._reply(200 if worker is not None and worker.is_alive() else 404)
            return
        worker, status = bot.get_or_start(channel)
        if worker is None:
            self._reply(status)
            return
        if not self._wait_for_first_byte(worker):
            self._reply(504)
            return
        viewer = worker.broadcaster.subscribe(config.LIVEBOT_MAX_VIEWERS)
        if viewer is None:
            throttled_log(logging.WARNING, f"viewers-{channel}", "Viewer limit of %s reached", channel, interval=60)
            self._reply(503)
            return
        try:
            self._stream(worker, viewer)
        finally:
            worker.broadcaster.unsubscribe(viewer)

    def _wait_for_first_byte(self, worker):
        deadline = time.monotonic() + FIRST_BYTE_TIMEOUT_SEC
        while not worker.broadcaster.first_byte_time:
            if time.monotonic() > deadline or not worker.is_alive() or self._client_gone():
                return False
            time.sleep(0.04)
        return worker.broadcaster.running

    def _client_gone(self):
        """True when the client closed the connection (it never sends a body)."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except ValueError:  # descriptor too large for select(): unknown
            return False
        except OSError:
            return True

    def _stream(self, worker, viewer):
        # No Content-Length: the closed socket is the only end marker. With keep-alive
        # the player would freeze on the last frame instead of reconnecting.
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        while True:
            try:
                chunk = viewer.get(timeout=2.0)
            except queue.Empty:
                # Ad break or stall: nothing to write, so look for a closed client.
                if not worker.broadcaster.running or self._client_gone():
                    return
                continue
            if chunk is None:
                return
            self.wfile.write(chunk)


class LiveHttpServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would let a second livebot take the same port.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, bot):
        self.bot = bot
        self._slots = threading.BoundedSemaphore(config.LIVEBOT_MAX_CHANNELS * config.LIVEBOT_MAX_VIEWERS + 16)
        super().__init__(address, LiveHttpHandler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            throttled_log(logging.WARNING, "http-slots", "Too many live connections, refusing one", interval=60)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except RuntimeError:  # no thread available; the handler never runs
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError):  # client went away or stopped reading
            logger.debug("Live client disconnected: %s", exc)
        else:
            logger.error("Live request failed", exc_info=True)


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - LIVEBOT - %(message)s", force=True
    )
    bot = LiveBot()

    def request_stop(_signum, _frame):
        # Event.set() takes a lock the interrupted main thread may hold right now.
        threading.Thread(target=bot.stop_event.set, daemon=True).start()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    if not config.LIVEBOT_ENABLED:
        # Stay up: an exit would be a restart loop under systemd/supervisor and a
        # degraded health check.
        logger.info("LIVEBOT_ENABLED is off; idle")
        while not bot.stop_event.wait(60):
            continue
        return 0
    try:
        server = LiveHttpServer((config.LIVEBOT_HOST, config.LIVEBOT_PORT), bot)
    except OSError as exc:
        logger.error("Cannot listen on %s:%s: %s", config.LIVEBOT_HOST, config.LIVEBOT_PORT, exc)
        return 1
    purge_leftovers(set())
    threading.Thread(target=server.serve_forever, daemon=True, name="live-http").start()
    logger.info(
        "livebot ready on %s:%s (max %s channels, %s viewers each)",
        config.LIVEBOT_HOST, config.LIVEBOT_PORT, config.LIVEBOT_MAX_CHANNELS, config.LIVEBOT_MAX_VIEWERS,
    )
    try:
        bot.run()
    finally:
        logger.info("livebot is shutting down")
        server.shutdown()
        bot.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
