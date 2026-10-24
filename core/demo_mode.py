#!/usr/bin/env python3
"""NachtSilo Hub - Turnkey Demo Mode Seeder & Mock Environment.

Provides realistic, neutral demo streamers, VODs, clips, chat replay, and live HLS
streams so the full application can be showcased and screenshotted out-of-the-box
without any external API keys or private credentials.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

from core import config
from core import database
from core import tube_auth

logger = logging.getLogger("demo_mode")

DEMO_STREAMERS = [
    {
        "name": "nexusprime",
        "display": "NexusPrime",
        "category": "Cyberpunk 2077",
        "title": "Night City 4K Max Settings · Secret Lore & Boss Builds",
        "live": 1,
        "record": 1,
        "avatar_color": "#4f46e5",
    },
    {
        "name": "codecraft",
        "display": "CodeCraft",
        "category": "Software & Game Dev",
        "title": "Live Coding: Building High-Concurrency Streaming Pipelines",
        "live": 1,
        "record": 1,
        "avatar_color": "#06b6d4",
    },
    {
        "name": "retroarcade",
        "display": "RetroArcade",
        "category": "Retro Gaming",
        "title": "Super Mario World Any% Glitchless PB Grind [Offline]",
        "live": 0,
        "record": 1,
        "avatar_color": "#f59e0b",
    },
]

DEMO_CHAT_MESSAGES = [
    ("NexusFan", "PogChamp what a clean shot!"),
    ("GamerX", "The lighting in this area is insane."),
    ("CyberSamurai", "LUL died to gravity again"),
    ("PixelQueen", "Can you show the build gear after this run?"),
    ("DevDan", "Great stream quality today, super smooth 60fps!"),
    ("NightOwl", "GG wp!"),
    ("ModMax", "Reminder: clip that boss fight guys!"),
    ("TechGeek", "The bitrate looks crystal clear on 1440p"),
    ("GlitchHunter", "Wait, check behind that pillar, hidden crate!"),
    ("Valkyrie", "hype hype hype"),
]


def ensure_sample_mp4(target_path: str, duration_sec: int = 15) -> bool:
    """Generate a clean, valid H.264/AAC MP4 video with moving test pattern and audio."""
    if os.path.isfile(target_path) and os.path.getsize(target_path) > 1024:
        return True
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i", f"testsrc=duration={duration_sec}:size=1280x720:rate=30",
        "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={duration_sec}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "veryfast",
        "-c:a", "aac",
        "-movflags", "+faststart",
        target_path,
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        logger.warning("ffmpeg not available or failed to create %s: %s", target_path, exc)
        return False


def ensure_thumbnail(video_path: str, thumb_path: str) -> bool:
    """Extract a thumbnail frame from an MP4 file."""
    if os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 100:
        return True
    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss", "00:00:02",
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        thumb_path,
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False


def generate_chat_json(target_path: str, duration_sec: int = 30) -> None:
    """Create a realistic .chat.json file synced with video playback."""
    if os.path.isfile(target_path):
        return
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    events = []
    base_ts = time.time() - 3600
    for idx, (user, msg) in enumerate(DEMO_CHAT_MESSAGES):
        offset = min(duration_sec - 1, idx * 2.5)
        events.append({
            "timestamp": base_ts + offset,
            "offset_sec": offset,
            "user": user,
            "message": msg,
            "color": "#6366f1" if idx % 2 == 0 else "#10b981",
            "badges": ["subscriber"] if idx % 3 == 0 else [],
        })
    with open(target_path, "w", encoding="utf-8") as handle:
        json.dump(events, handle, indent=2)


def ensure_live_hls(live_dir: str, duration_sec: int = 15) -> bool:
    """Generate a valid looping HLS stream in LIVE_HLS_ROOT for live player testing."""
    os.makedirs(live_dir, exist_ok=True)
    playlist = os.path.join(live_dir, "index.m3u8")
    if os.path.isfile(playlist):
        return True
    cmd = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i", f"testsrc=duration={duration_sec}:size=1280x720:rate=30",
        "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={duration_sec}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "veryfast",
        "-c:a", "aac",
        "-hls_time", "2",
        "-hls_list_size", "5",
        "-hls_flags", "delete_segments",
        playlist,
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False


def seed_demo_environment() -> dict:
    """Initialize neutral demo database, channels, VODs, clips, and traffic."""
    database.init_db()
    conn = database.get_db()
    seeded = {"channels": 0, "users": 0, "vods": 0}
    try:
        # 1. Channels
        for st in DEMO_STREAMERS:
            conn.execute(
                """
                INSERT OR REPLACE INTO channels (name, record, live, chat, platform, source_url, archive_name, notify)
                VALUES (?, ?, ?, ?, 'twitch', ?, ?, 1)
                """,
                (
                    st["name"],
                    st["record"],
                    st["live"],
                    st["record"],
                    f"https://twitch.tv/{st['name']}",
                    st["name"],
                ),
            )
            # Seed drive folder mock if needed
            conn.execute(
                """
                INSERT OR IGNORE INTO drive_folders (streamer, folder_id, visibility, title, updated_at)
                VALUES (?, ?, 'members', ?, ?)
                """,
                (st["name"], f"mock_folder_{st['name']}", st["display"], time.time()),
            )
            seeded["channels"] += 1

        # 2. Users
        demo_users = [
            ("admin", "admin", "admin"),
            ("viewer", "viewer", "viewer"),
            ("member", "member", "member"),
        ]
        for username, password, role in demo_users:
            p_hash = generate_password_hash(password)
            now = time.time()
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    username, password_hash, role, active, created_via, created_at,
                    rules_accepted_at, rules_version, terms_accepted_at, terms_version,
                    last_seen_at, last_login_at
                ) VALUES (?, ?, ?, 1, 'demo', ?, ?, 'v1.0', ?, 'v1.0', ?, ?)
                """,
                (username, p_hash, role, now, now, now, now, now),
            )
            seeded["users"] += 1

        # 3. Traffic Data (last 14 days)
        today = datetime.now()
        for day_offset in range(14):
            day_str = (today - timedelta(days=day_offset)).strftime("%Y-%m-%d")
            for username in ("admin", "viewer"):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO user_traffic_daily (username, day, logins, pages, media, chats)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        username,
                        day_str,
                        max(1, 5 - day_offset // 3),
                        max(4, 25 - day_offset),
                        max(2, 12 - day_offset // 2),
                        max(0, 8 - day_offset // 2),
                    ),
                )

        conn.commit()
    finally:
        conn.close()

    # 4. Generate Physical Files
    files_dir = config.FILES_DIR
    meta_dir = config.META_DIR
    thumb_dir = config.THUMB_ROOT
    live_root = config.LIVE_HLS_ROOT

    os.makedirs(files_dir, exist_ok=True)
    os.makedirs(meta_dir, exist_ok=True)
    os.makedirs(thumb_dir, exist_ok=True)
    os.makedirs(live_root, exist_ok=True)

    # VOD samples
    sample_vods = [
        ("nexusprime", "nexusprime_tw_2026-10-24_10-00-00.mp4", "Night City 4K · Secret Lore & Boss Builds", 30),
        ("codecraft", "codecraft_tw_2026-10-24_08-30-00.mp4", "Live Coding: High-Concurrency Video Pipelines", 30),
        ("retroarcade", "retroarcade_tw_2026-10-23_18-00-00.mp4", "Super Mario World Any% Glitchless PB Attempt", 30),
    ]

    for streamer, filename, title, duration in sample_vods:
        streamer_dir = os.path.join(files_dir, streamer)
        video_path = os.path.join(streamer_dir, filename)
        chat_path = os.path.join(streamer_dir, filename.replace(".mp4", ".chat.json"))
        thumb_path = os.path.join(thumb_dir, streamer, f"{filename}.jpg")
        meta_path = os.path.join(meta_dir, f"{filename}.json")

        if ensure_sample_mp4(video_path, duration_sec=duration):
            ensure_thumbnail(video_path, thumb_path)
            generate_chat_json(chat_path, duration_sec=duration)
            
            # Write metadata json
            meta_payload = {
                "title": title,
                "streamer": streamer,
                "recorded_at": time.time() - 7200,
                "duration_sec": duration,
                "duration_str": f"00:00:{duration:02d}",
                "views": 42,
            }
            try:
                with open(meta_path, "w", encoding="utf-8") as h:
                    json.dump(meta_payload, h, indent=2)
            except Exception:
                pass
            seeded["vods"] += 1

    # 5. Live HLS for active streams
    for streamer_info in DEMO_STREAMERS:
        if streamer_info["live"]:
            s_live_dir = os.path.join(live_root, streamer_info["name"])
            ensure_live_hls(s_live_dir, duration_sec=10)

    # 6. Sample Clip
    clips_dir = os.path.join(files_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)
    sample_clip_path = os.path.join(clips_dir, "clip_nexusprime_bossfight.mp4")
    ensure_sample_mp4(sample_clip_path, duration_sec=10)

    return seeded
