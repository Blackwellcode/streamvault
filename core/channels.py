"""Channel lookups shared by the web UI and the recorder.

Both processes used to carry identical copies of these helpers. They only read
the ``channels`` table and know which hosts count as YouTube; no Flask, no
recorder state.
"""

from __future__ import annotations

from core import database

YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})


def is_youtube_host(hostname) -> bool:
    return str(hostname or "").lower() in YOUTUBE_HOSTS


def load_channels() -> list[str]:
    """Channels with recording enabled."""
    return database.get_channels(mode="record")


def load_live_channels() -> list[str]:
    """Channels with live restreaming enabled."""
    return database.get_channels(mode="live")


def load_all_channels() -> list[str]:
    return database.get_channels(mode="all")
