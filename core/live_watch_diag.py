#!/usr/bin/env python3
"""Live-watch diagnostics shared by CLI, health audit, and admin API.

Never includes tokens, cookies, or env secrets.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from core import config

logger = logging.getLogger(__name__)

LIVE_HLS_TTL_SEC = int(os.environ.get("LIVE_HLS_TTL_SEC", "24"))
LIVE_WATCH_DEMAND_SEC = int(os.environ.get("LIVE_WATCH_DEMAND_SEC", "120"))
LIVE_STALL_RESTART_SEC = int(os.environ.get("LIVE_STALL_RESTART_SEC", "12"))
CLIENT_DEBUG_PATH = Path(config.META_DIR) / "live_watch_client.jsonl"
CLIENT_DEBUG_KEEP = 500


def _age(path: str | Path, now: float | None = None) -> float | None:
    now = time.time() if now is None else now
    try:
        return round(now - os.path.getmtime(path), 2)
    except OSError:
        return None


def _tail_text(path: str | Path, max_lines: int = 40, max_bytes: int = 12000) -> str:
    try:
        data = Path(path).read_bytes()[-max_bytes:]
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def _scan_log_issues(text: str) -> list[str]:
    if not text:
        return []
    patterns = (
        r"(?i)error",
        r"(?i)failed",
        r"(?i)unable",
        r"(?i)403",
        r"(?i)404",
        r"(?i)429",
        r"(?i)offline",
        r"(?i)no playable streams",
        r"(?i)broken pipe",
        r"(?i)connection reset",
        r"(?i)Invalid data",
        r"(?i)Non-monotonous",
    )
    hits = []
    for line in text.splitlines():
        if any(re.search(p, line) for p in patterns):
            hits.append(line.strip()[:240])
    return hits[-12:]


def list_candidate_channels(extra: list[str] | None = None) -> list[str]:
    found: set[str] = set()
    live_root = Path(config.LIVE_HLS_ROOT)
    if live_root.is_dir():
        for child in live_root.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                found.add(child.name.lower())
    demand_dir = Path(config.META_DIR) / "live_watch_demand"
    if demand_dir.is_dir():
        for child in demand_dir.glob("*.ts"):
            found.add(child.stem.lower())
    try:
        from core import database

        for conf in database.list_channel_configs(mode="record") or []:
            name = str(conf.get("name") or "").strip().lower()
            if name:
                found.add(name)
            if int(conf.get("live") or 0) == 1 and name:
                found.add(name)
    except database.DB_ACCESS_ERRORS as exc:
        logger.warning("live diag: channel configs unavailable: %s", exc)
    for name in extra or []:
        if name:
            found.add(str(name).strip().lower())
    return sorted(found)


def streamlink_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        out = subprocess.check_output(["ps", "aux"], text=True, timeout=8)
    except Exception:
        return counts
    for line in out.splitlines():
        if "streamlink" not in line or "twitch.tv/" not in line:
            continue
        for part in line.split():
            if "twitch.tv/" in part:
                ch = part.rstrip("/").split("/")[-1].lower()
                counts[ch] = counts.get(ch, 0) + 1
    return counts


def ffmpeg_hls_pids() -> dict[str, int]:
    """channel -> ffmpeg pid writing live HLS index."""
    found: dict[str, int] = {}
    try:
        out = subprocess.check_output(["ps", "aux"], text=True, timeout=8)
    except Exception:
        return found
    hls_root = str(config.LIVE_HLS_ROOT).rstrip("/")
    pattern = re.compile(re.escape(hls_root) + r"/([^/\s]+)/index\.m3u8")
    for line in out.splitlines():
        if "ffmpeg" not in line or hls_root not in line:
            continue
        if "index.m3u8" not in line:
            continue
        m = pattern.search(line)
        if not m:
            continue
        parts = line.split()
        try:
            pid = int(parts[1])
        except (IndexError, ValueError):
            continue
        found[m.group(1).lower()] = pid
    return found


def diagnose_channel(channel: str, *, logs: int = 30, helix: dict | None = None) -> dict[str, Any]:
    channel = str(channel or "").strip().lower()
    now = time.time()
    live_dir = Path(config.LIVE_HLS_ROOT) / channel
    playlist = live_dir / "index.m3u8"
    heartbeat = live_dir / ".heartbeat"
    demand = Path(config.META_DIR) / "live_watch_demand" / f"{channel}.ts"
    ffmpeg_log = live_dir / "ffmpeg.log"
    streamlink_log = live_dir / "streamlink.log"

    segs = []
    newest_seg_age = None
    seg_bytes = []
    if live_dir.is_dir():
        for name in sorted(live_dir.glob("seg_*.ts")):
            try:
                st = name.stat()
            except OSError:
                continue
            segs.append(name.name)
            seg_bytes.append(st.st_size)
            age = now - st.st_mtime
            if newest_seg_age is None or age < newest_seg_age:
                newest_seg_age = age

    playlist_age = _age(playlist, now) if playlist.is_file() else None
    fresh = False
    if playlist_age is not None and playlist_age <= LIVE_HLS_TTL_SEC or newest_seg_age is not None and newest_seg_age <= LIVE_HLS_TTL_SEC:
        fresh = True

    db_live = None
    db_record = None
    try:
        from core import database

        conf = database.get_channel_config(channel) or {}
        if conf:
            db_live = int(conf.get("live") or 0)
            db_record = int(conf.get("record") or 0)
    except database.DB_ACCESS_ERRORS as exc:
        logger.warning("live diag %s: channel config unavailable: %s", channel, exc)

    helix_info = (helix or {}).get(channel) if helix is not None else None
    if helix_info is None:
        try:
            from core import twitch_api

            helix_info = twitch_api.get_stream(channel, monitored=True)
        except Exception as exc:
            helix_info = {"error": str(exc)[:200]}

    hb_age = _age(heartbeat, now) if heartbeat.is_file() else None
    demand_age = _age(demand, now) if demand.is_file() else None
    has_demand = any(
        age is not None and age <= LIVE_WATCH_DEMAND_SEC for age in (hb_age, demand_age)
    )

    sl_counts = streamlink_counts()
    ff_pids = ffmpeg_hls_pids()
    ff_tail = _tail_text(ffmpeg_log, max_lines=logs)
    sl_tail = _tail_text(streamlink_log, max_lines=max(8, logs // 2))

    issues = []
    if has_demand and not fresh:
        issues.append("demand_without_fresh_hls")
    if helix_info and helix_info.get("is_live") and has_demand and not fresh:
        issues.append("helix_live_and_demand_but_hls_stale")
    if playlist_age is not None and playlist_age > LIVE_STALL_RESTART_SEC:
        issues.append("playlist_older_than_stall_window")
    if sl_counts.get(channel, 0) >= 2:
        issues.append("dual_streamlink_ingest")
    if has_demand and channel not in ff_pids and not fresh:
        issues.append("no_ffmpeg_hls_worker")
    if db_live == 1 and not has_demand and not fresh:
        issues.append("sticky_live_flag_without_demand")

    avg_seg = round(sum(seg_bytes) / len(seg_bytes)) if seg_bytes else None
    return {
        "channel": channel,
        "fresh": fresh,
        "issues": issues,
        "paths": {
            "live_dir": str(live_dir),
            "playlist": str(playlist) if playlist.is_file() else None,
            "heartbeat": str(heartbeat) if heartbeat.is_file() else None,
            "demand": str(demand) if demand.is_file() else None,
        },
        "ages_sec": {
            "playlist": playlist_age,
            "newest_seg": round(newest_seg_age, 2) if newest_seg_age is not None else None,
            "heartbeat": hb_age,
            "demand": demand_age,
        },
        "segments": {
            "count": len(segs),
            "newest": segs[-1] if segs else None,
            "avg_bytes": avg_seg,
            "approx_kbps": round((avg_seg or 0) * 8 / max(int(os.environ.get("LIVE_HLS_TIME", "2")), 1) / 1000, 1)
            if avg_seg
            else None,
        },
        "db": {"live": db_live, "record": db_record},
        "demand_active": has_demand,
        "helix": {
            "is_live": bool((helix_info or {}).get("is_live")),
            "title": ((helix_info or {}).get("title") or "")[:120],
            "viewer_count": (helix_info or {}).get("viewer_count"),
            "error": (helix_info or {}).get("error"),
        },
        "procs": {
            "streamlink": sl_counts.get(channel, 0),
            "ffmpeg_hls_pid": ff_pids.get(channel),
        },
        "logs": {
            "ffmpeg_issues": _scan_log_issues(ff_tail),
            "streamlink_issues": _scan_log_issues(sl_tail),
            "ffmpeg_tail": ff_tail[-2000:],
            "streamlink_tail": sl_tail[-1200:],
        },
        "thresholds": {
            "LIVE_HLS_TTL_SEC": LIVE_HLS_TTL_SEC,
            "LIVE_WATCH_DEMAND_SEC": LIVE_WATCH_DEMAND_SEC,
            "LIVE_STALL_RESTART_SEC": LIVE_STALL_RESTART_SEC,
        },
    }


def collect_report(
    channels: list[str] | None = None,
    *,
    logs: int = 24,
    include_supervisor: bool = True,
) -> dict[str, Any]:
    now = time.time()
    selected = list_candidate_channels(channels)
    if channels:
        wanted = {str(c).strip().lower() for c in channels if c}
        selected = [c for c in selected if c in wanted] or sorted(wanted)

    helix_batch = {}
    try:
        from core import twitch_api

        helix_batch = twitch_api.get_streams_by_logins(selected, monitored=True) if selected else {}
    except Exception as exc:
        helix_batch = {"_error": str(exc)[:200]}

    channel_reports = []
    for ch in selected:
        hx = helix_batch if "_error" not in helix_batch else None
        channel_reports.append(diagnose_channel(ch, logs=logs, helix=hx if isinstance(hx, dict) else None))

    supervisor = []
    if include_supervisor:
        try:
            out = subprocess.check_output(["supervisorctl", "status"], text=True, timeout=10)
            for line in out.splitlines():
                if "livebot" in line or "webui" in line or "recorder" in line or "healthmon" in line:
                    supervisor.append(line.strip())
        except Exception as exc:
            supervisor.append(f"supervisorctl failed: {exc}")

    issue_count = sum(len(c.get("issues") or []) for c in channel_reports)
    fresh_count = sum(1 for c in channel_reports if c.get("fresh"))
    demand_count = sum(1 for c in channel_reports if c.get("demand_active"))

    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "epoch": int(now),
        "summary": {
            "channels": len(channel_reports),
            "fresh_hls": fresh_count,
            "active_demand": demand_count,
            "issue_flags": issue_count,
            "dual_ingest": [
                c["channel"] for c in channel_reports if "dual_streamlink_ingest" in (c.get("issues") or [])
            ],
        },
        "supervisor": supervisor,
        "streamlink_procs": streamlink_counts(),
        "channels": channel_reports,
        "client_debug_tail": read_client_debug_tail(12),
    }


def append_client_debug(event: dict[str, Any]) -> None:
    """Append a sanitized client debug event (proxy player telemetry)."""
    Path(config.META_DIR).mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "epoch": time.time(),
        "channel": str(event.get("channel") or "")[:64],
        "mode": str(event.get("mode") or "")[:32],
        "level": str(event.get("level") or "info")[:16],
        "event": str(event.get("event") or "")[:64],
        "detail": str(event.get("detail") or "")[:500],
        "ready": bool(event.get("ready")),
        "waited_sec": event.get("waited_sec"),
        "fatal_type": str(event.get("fatal_type") or "")[:64],
        "fatal_details": str(event.get("fatal_details") or "")[:240],
        "reconnect_attempt": event.get("reconnect_attempt"),
        "user_agent": str(event.get("user_agent") or "")[:160],
    }
    with open(CLIENT_DEBUG_PATH, "a", encoding="utf-8") as handle:
        import json

        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    try:
        lines = CLIENT_DEBUG_PATH.read_text(encoding="utf-8").splitlines()
        if len(lines) > CLIENT_DEBUG_KEEP:
            CLIENT_DEBUG_PATH.write_text("\n".join(lines[-CLIENT_DEBUG_KEEP:]) + "\n", encoding="utf-8")
        st = Path(config.META_DIR).stat()
        os.chown(CLIENT_DEBUG_PATH, st.st_uid, st.st_gid)
        os.chmod(CLIENT_DEBUG_PATH, 0o640)
    except OSError:
        pass


def read_client_debug_tail(n: int = 20) -> list[dict]:
    import json

    if not CLIENT_DEBUG_PATH.is_file():
        return []
    try:
        lines = CLIENT_DEBUG_PATH.read_text(encoding="utf-8").splitlines()[-max(1, n) :]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def human_print(report: dict[str, Any]) -> str:
    lines = []
    s = report.get("summary") or {}
    lines.append(
        f"Live-watch debug @ {report.get('ts')} | channels={s.get('channels')} "
        f"fresh={s.get('fresh_hls')} demand={s.get('active_demand')} issues={s.get('issue_flags')}"
    )
    if s.get("dual_ingest"):
        lines.append(f"Dual ingest: {', '.join(s['dual_ingest'])}")
    for line in report.get("supervisor") or []:
        lines.append(f"  {line}")
    for ch in report.get("channels") or []:
        ages = ch.get("ages_sec") or {}
        lines.append(
            f"- {ch.get('channel')}: fresh={ch.get('fresh')} demand={ch.get('demand_active')} "
            f"helix_live={(ch.get('helix') or {}).get('is_live')} "
            f"segs={(ch.get('segments') or {}).get('count')} "
            f"pl_age={ages.get('playlist')} seg_age={ages.get('newest_seg')} "
            f"sl={(ch.get('procs') or {}).get('streamlink')} "
            f"issues={','.join(ch.get('issues') or []) or '-'}"
        )
        for issue in (ch.get("logs") or {}).get("ffmpeg_issues") or []:
            lines.append(f"    ffmpeg: {issue}")
        for issue in (ch.get("logs") or {}).get("streamlink_issues") or []:
            lines.append(f"    streamlink: {issue}")
    clients = report.get("client_debug_tail") or []
    if clients:
        lines.append("Client debug (tail):")
        for ev in clients[-8:]:
            lines.append(
                f"  {ev.get('ts')} {ev.get('channel')} {ev.get('event')} "
                f"{ev.get('detail') or ev.get('fatal_type') or ''}"
            )
    return "\n".join(lines)
