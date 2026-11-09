#!/usr/bin/env python3
"""Create a small demo archive so StreamVault can be tried without recording.

Generates short MP4 recordings (FFmpeg test sources, no third-party content)
and matching chat logs for a few fictional channels:

    python scripts/demo_seed.py --base-dir ./demo-data
    VOD_BASE_DIR=./demo-data python -m WEBUI.webui

Existing files are never overwritten. The target directory must be empty or a
directory created by an earlier run of this script.
"""

import argparse
import json
import logging
import os
import random
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

logger = logging.getLogger("demo_seed")

MARKER_FILE = ".streamvault-demo"
PLATFORMS = {"tw": "twitch", "yt": "youtube"}
CHAT_LOG_SUBDIR = os.environ.get("VOD_CHAT_SUBDIR", "chat_logs")
FFMPEG_TIMEOUT_SEC = 300

# name, platform tag, category, gradient colours, stream titles (newest first)
CHANNELS = [
    ("pixelharbor", "tw", "Retro", ("0x1e1b4b", "0x7c3aed", "0x0ea5e9"),
     ["Speedrun practice: any% routes", "Retro Sunday: 16-bit classics"]),
    ("lunarlab", "yt", "Music", ("0x022c22", "0x059669", "0xfacc15"),
     ["Building a synth from scratch", "Q&A and modular patching"]),
    ("tidecraft", "tw", "Simulation", ("0x431407", "0xea580c", "0xf472b6"),
     ["Cozy farming, day 112", "Island tour and base building"]),
]

CHATTERS = [
    ("mossbyte", "#22c55e"), ("quietfox", "#f97316"), ("neonwren", "#a855f7"),
    ("drift_owl", "#38bdf8"), ("pebble42", "#facc15"), ("saltyhare", "#f43f5e"),
    ("kilnmoth", "#14b8a6"), ("orbitpine", "#818cf8"),
]

MESSAGES = [
    "hello chat!", "that was clean", "GG", "first time here, love the vibe",
    "what settings are you using?", "haha", "no way", "chat is flying today",
    "can you show that again?", "hydrate check", "this song is great",
    "W", "how long have you been streaming?", "clip that", "let's go",
    "the new overlay looks good", "brb grabbing tea", "nice save",
]


def _fontfile_option():
    """Return a drawtext fontfile option for a sans font, or '' for the default."""
    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            # drawtext option values treat ':' as a separator.
            escaped = path.replace("\\", "/").replace(":", "\\:")
            return f":fontfile='{escaped}'"
    return ""


def _escape_drawtext(text):
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\u2019")


def render_video(ffmpeg, path, title, channel, colors, duration):
    font = _fontfile_option()
    c0, c1, c2 = colors
    label = _escape_drawtext(title)
    sub = _escape_drawtext(channel)
    tmp_path = path + ".part"
    # expansion=none: titles may contain '%', which drawtext would parse as a function.
    video_filter = (
        f"drawtext=text='{label}':expansion=none{font}:fontsize=64:fontcolor=white:"
        "x=(w-tw)/2:y=(h-th)/2-30:shadowcolor=black@0.4:shadowx=2:shadowy=2,"
        f"drawtext=text='{sub}':expansion=none{font}:fontsize=34:fontcolor=white@0.75:"
        "x=(w-tw)/2:y=(h-th)/2+50"
    )
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"gradients=s=1280x720:r=30:c0={c0}:c1={c1}:c2={c2}:n=3:speed=0.01:d={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=48000:d={duration}",
        "-vf", video_filter,
        "-af", "volume=0.05",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart", "-shortest",
        "-f", "mp4", tmp_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SEC)
    if result.returncode != 0:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise RuntimeError(f"ffmpeg failed for {path}: {result.stderr.strip()[-500:]}")
    # Only a finished file gets the final name, so a failed run is retried next time.
    os.replace(tmp_path, path)


def render_thumbnail(ffmpeg, video_path, thumb_path):
    """Pre-render the card thumbnail so the demo looks complete without the worker."""
    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", "3", "-i", video_path, "-frames:v", "1",
        "-vf", "scale=640:-2", "-q:v", "3", thumb_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SEC)
    if result.returncode != 0:
        raise RuntimeError(f"thumbnail failed for {video_path}: {result.stderr.strip()[-500:]}")


def render_avatar(ffmpeg, path, channel, colors):
    """Local avatar, so the UI never pulls the profile picture of a real account with the same name."""
    font = _fontfile_option()
    c0, c1, c2 = colors
    initial = _escape_drawtext(channel[:1].upper())
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"gradients=s=256x256:c0={c0}:c1={c1}:c2={c2}:n=3:d=1",
        "-vf", f"drawtext=text='{initial}':expansion=none{font}:fontsize=120:fontcolor=white:"
               "x=(w-tw)/2:y=(h-th)/2",
        "-frames:v", "1", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SEC)
    if result.returncode != 0:
        raise RuntimeError(f"avatar failed for {channel}: {result.stderr.strip()[-500:]}")


def write_chat_log(path, start, duration, rng):
    lines = []
    rel = 1.0
    while rel < duration:
        user, color = rng.choice(CHATTERS)
        entry = {
            "t": round(start.timestamp() + rel, 3),
            "rel": round(rel, 3),
            "user": user,
            "msg": rng.choice(MESSAGES),
            "color": color,
        }
        lines.append(json.dumps(entry, ensure_ascii=False))
        rel += rng.uniform(0.6, 2.4)
    with open(path, "x", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return len(lines)


def check_target(base_dir):
    if not os.path.isdir(base_dir):
        os.makedirs(base_dir)
        return
    entries = [name for name in os.listdir(base_dir) if name != MARKER_FILE]
    if entries and not os.path.isfile(os.path.join(base_dir, MARKER_FILE)):
        raise SystemExit(
            f"{base_dir} is not empty and was not created by this script; "
            "choose an empty directory so real archives are never mixed with demo data."
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create a demo archive for trying StreamVault.")
    parser.add_argument("--base-dir", default=os.environ.get("VOD_BASE_DIR") or "demo-data",
                        help="archive root to fill (default: $VOD_BASE_DIR or ./demo-data)")
    parser.add_argument("--duration", type=int, default=90,
                        help="length of each demo recording in seconds (default: 90)")
    parser.add_argument("--seed", type=int, default=7, help="random seed for chat logs")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.error("ffmpeg was not found on PATH")
        return 1
    if args.duration < 5:
        logger.error("--duration must be at least 5 seconds")
        return 1

    base_dir = os.path.abspath(args.base_dir)
    check_target(base_dir)
    with open(os.path.join(base_dir, MARKER_FILE), "w", encoding="utf-8") as handle:
        handle.write("Created by scripts/demo_seed.py\n")

    thumb_root = os.environ.get("VOD_THUMB_DIR") or os.path.join(base_dir, ".thumbs")
    rng = random.Random(args.seed)
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    created = 0
    for index, (channel, platform, category, colors, titles) in enumerate(CHANNELS):
        channel_dir = os.path.join(base_dir, channel)
        chat_dir = os.path.join(channel_dir, CHAT_LOG_SUBDIR)
        os.makedirs(chat_dir, exist_ok=True)
        avatar_path = os.path.join(channel_dir, "avatar.png")
        if not os.path.exists(avatar_path):
            render_avatar(ffmpeg, avatar_path, channel, colors)
        for age, title in enumerate(titles):
            start = now - timedelta(days=age * 3 + index, hours=index * 2 + 3)
            stem = f"{channel}_{platform}_{start:%Y-%m-%d_%H-%M-%S}"
            video_path = os.path.join(channel_dir, f"{stem}.mp4")
            chat_path = os.path.join(chat_dir, f"{stem}.jsonl")
            thumb_path = os.path.join(thumb_root, channel, f"{stem}.jpg")
            info_path = os.path.join(channel_dir, f"{stem}.info.json")
            if not os.path.exists(info_path):
                # Same sidecar format yt-dlp writes; the web UI reads title and category from it.
                with open(info_path, "x", encoding="utf-8") as handle:
                    json.dump({"title": title, "category": category, "platform": PLATFORMS[platform]},
                              handle, ensure_ascii=False)
            if os.path.exists(video_path):
                logger.info("skip existing %s", video_path)
                if not os.path.exists(thumb_path):
                    render_thumbnail(ffmpeg, video_path, thumb_path)
                continue
            render_video(ffmpeg, video_path, title, channel, colors, args.duration)
            render_thumbnail(ffmpeg, video_path, thumb_path)
            messages = write_chat_log(chat_path, start, args.duration, rng)
            created += 1
            logger.info("created %s (%d chat messages)", os.path.relpath(video_path, base_dir), messages)

    logger.info("%d recordings created in %s", created, base_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
