"""Concurrent VOD watcher slots (shared via SQLite, not Live HLS).

A slot is one person actually pulling VOD bytes. Live HLS is unlimited.

Lifecycle
---------
- Claimed on first real VOD media request or explicit /api/vod/slot.
- Heartbeat every ~15s while the player page is open (playing or paused).
- Pause / seek: keep the seat so they can resume later. Do not release.
- Leave page / video ended: immediate release.
- Crash / killed tab / no heartbeat: row is stale after VOD_SLOT_STALE_SEC
  (90s) and deleted on the next claim, heartbeat, or snapshot.
- Same account switching VODs reuses one slot.
- 429 Retry-After is seconds until the oldest seat would go stale — the
  soonest a crashed watcher can free a slot. Active/paused watchers keep
  resetting that clock; the busy text tells people to retry in a few minutes.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

_CLAIM_CACHE = {}  # (holder.lower(), streamer, filename) -> (stamp, active)
_CLAIM_LOCK = threading.Lock()

from core import config
from core import database

BUSY_CODE = "vod_slots_full"
BUSY_MESSAGE = (
    "Gerade laufen zu viele VODs. Live ist davon nicht betroffen. "
    "Versuch es in ein paar Minuten nochmal — der Platz wird frei, "
    "sobald jemand den Player schließt."
)


def max_watchers():
    try:
        return max(0, int(getattr(config, "VOD_MAX_CONCURRENT_WATCHERS", 20)))
    except (TypeError, ValueError):
        return 20


def stale_sec():
    try:
        return max(30, int(getattr(config, "VOD_SLOT_STALE_SEC", 90)))
    except (TypeError, ValueError):
        return 90


def pause_grace_sec():
    try:
        return max(30, int(getattr(config, "VOD_SLOT_PAUSE_GRACE_SEC", 240)))
    except (TypeError, ValueError):
        return 240


def heartbeat_sec():
    try:
        return max(5, int(getattr(config, "VOD_SLOT_HEARTBEAT_SEC", 15)))
    except (TypeError, ValueError):
        return 15


def public_limits():
    return {
        "max": max_watchers(),
        "stale_sec": stale_sec(),
        "pause_grace_sec": pause_grace_sec(),
        "heartbeat_sec": heartbeat_sec(),
    }


def _payload(ok, *, holder="", active=0, reason="", retry_after=0):
    limits = public_limits()
    data = {
        "ok": bool(ok),
        "code": "" if ok else (reason or BUSY_CODE),
        "message": "" if ok else BUSY_MESSAGE,
        "active": int(active),
        "holder": holder,
        "retry_after": int(retry_after or (0 if ok else limits["stale_sec"])),
    }
    data.update(limits)
    return data


def _purge_stale(conn, now, stale):
    conn.execute("DELETE FROM vod_watch_slots WHERE last_seen_at < ?", (now - stale,))


def _count(conn):
    row = conn.execute("SELECT COUNT(*) AS n FROM vod_watch_slots").fetchone()
    return int(row["n"] if row else 0)


def _retry_after(conn, now, stale):
    """Seconds until the oldest live seat would be purged as stale."""
    row = conn.execute("SELECT MIN(last_seen_at) AS oldest FROM vod_watch_slots").fetchone()
    oldest = None if row is None else row["oldest"]
    if oldest is None:
        return int(stale)
    remaining = float(stale) - (float(now) - float(oldest))
    return max(5, int(remaining + 0.999))


def claim(holder, streamer="", filename="", *, now=None):
    holder = str(holder or "").strip()
    if not holder:
        return _payload(False, reason="vod_slot_anonymous", retry_after=5)
    cap = max_watchers()
    if cap <= 0:
        return _payload(True, holder=holder, active=0)
    stamp = time.time() if now is None else float(now)

    # 15s in-memory TTL: bypass SQLite transaction on repeat byte-range requests
    cache_key = (holder.lower(), str(streamer or "").strip(), str(filename or "").strip())
    with _CLAIM_LOCK:
        cached_stamp, cached_active = _CLAIM_CACHE.get(cache_key, (0, 0))
        if stamp - cached_stamp < 15.0:
            return _payload(True, holder=holder, active=cached_active)

    stale = stale_sec()
    conn = database.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _purge_stale(conn, stamp, stale)
        existing = conn.execute(
            "SELECT holder FROM vod_watch_slots WHERE holder = ? COLLATE NOCASE",
            (holder,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE vod_watch_slots
                SET username=?, streamer=?, filename=?, last_seen_at=?
                WHERE holder=? COLLATE NOCASE
                """,
                (holder, str(streamer or ""), str(filename or ""), stamp, holder),
            )
            active = _count(conn)
            conn.commit()
            with _CLAIM_LOCK:
                _CLAIM_CACHE[cache_key] = (stamp, active)
            return _payload(True, holder=holder, active=active)
        active = _count(conn)
        if active >= cap:
            retry_after = _retry_after(conn, stamp, stale)
            conn.commit()
            return _payload(False, active=active, reason=BUSY_CODE, retry_after=retry_after)
        conn.execute(
            """
            INSERT INTO vod_watch_slots (holder, username, streamer, filename, acquired_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (holder, holder, str(streamer or ""), str(filename or ""), stamp, stamp),
        )
        active = _count(conn)
        conn.commit()
        with _CLAIM_LOCK:
            _CLAIM_CACHE[cache_key] = (stamp, active)
        return _payload(True, holder=holder, active=active)
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def heartbeat(holder, *, now=None):
    holder = str(holder or "").strip()
    if not holder:
        return _payload(False, reason="vod_slot_anonymous", retry_after=5)
    cap = max_watchers()
    if cap <= 0:
        return _payload(True, holder=holder, active=0)
    stamp = time.time() if now is None else float(now)
    conn = database.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _purge_stale(conn, stamp, stale_sec())
        cur = conn.execute(
            "UPDATE vod_watch_slots SET last_seen_at=? WHERE holder=? COLLATE NOCASE",
            (stamp, holder),
        )
        if cur.rowcount:
            active = _count(conn)
            conn.commit()
            with _CLAIM_LOCK:
                h_low = holder.lower()
                for k in list(_CLAIM_CACHE.keys()):
                    if k[0] == h_low:
                        _CLAIM_CACHE[k] = (stamp, active)
            return _payload(True, holder=holder, active=active)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()
    return claim(holder, now=stamp)


def release(holder):
    holder = str(holder or "").strip()
    if not holder:
        return _payload(True, active=0)
    with _CLAIM_LOCK:
        h_low = holder.lower()
        for k in list(_CLAIM_CACHE.keys()):
            if k[0] == h_low:
                _CLAIM_CACHE.pop(k, None)
    conn = database.get_db()
    try:
        conn.execute("DELETE FROM vod_watch_slots WHERE holder=? COLLATE NOCASE", (holder,))
        active = _count(conn)
        conn.commit()
        return _payload(True, holder=holder, active=active)
    finally:
        conn.close()


def snapshot():
    cap = max_watchers()
    stale = stale_sec()
    now = time.time()
    conn = database.get_db()
    try:
        _purge_stale(conn, now, stale)
        conn.commit()
        active = _count(conn)
    finally:
        conn.close()
    return _payload(active < cap or cap <= 0, active=active)


def is_vod_watched(streamer, filename, *, now=None):
    """True if any live watch-slot currently holds this VOD."""
    streamer = str(streamer or "").strip()
    filename = os.path.basename(str(filename or "").strip())
    if not streamer or not filename:
        return False
    stamp = time.time() if now is None else float(now)
    conn = database.get_db()
    try:
        _purge_stale(conn, stamp, stale_sec())
        conn.commit()
        row = conn.execute(
            """
            SELECT 1 AS ok FROM vod_watch_slots
            WHERE streamer = ? COLLATE NOCASE AND filename = ? COLLATE NOCASE
            LIMIT 1
            """,
            (streamer, filename),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def list_watched_vods(*, now=None):
    """Return ``{(streamer_lower, filename_lower), ...}`` for active seats."""
    stamp = time.time() if now is None else float(now)
    conn = database.get_db()
    try:
        _purge_stale(conn, stamp, stale_sec())
        conn.commit()
        rows = conn.execute(
            "SELECT streamer, filename FROM vod_watch_slots"
        ).fetchall()
    finally:
        conn.close()
    out = set()
    for row in rows or []:
        streamer = str(row["streamer"] or "").strip().lower()
        filename = os.path.basename(str(row["filename"] or "").strip()).lower()
        if streamer and filename:
            out.add((streamer, filename))
    return out
