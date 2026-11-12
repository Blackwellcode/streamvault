"""
High-Performance VOD Chat Chunking Engine for StreamVault.
Splits linear JSONL chat logs into static 10-minute JSON blocks (600 seconds)
that can be served directly by Nginx via X-Accel-Redirect and cached by browsers.
"""

import os
import json
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger("streamvault.chat_chunker")

CHUNK_DURATION_SEC = 600  # 10 minutes per chunk
CHAT_CHUNKS_DIRNAME = "chat_chunks"


def get_chunks_dir(base_data_dir: str, streamer: str, vod_filename: str) -> str:
    """Get the directory where chunks for a specific VOD are stored."""
    base_name = os.path.splitext(os.path.basename(vod_filename))[0]
    return os.path.join(base_data_dir, streamer, CHAT_CHUNKS_DIRNAME, base_name)


def ensure_chat_chunks(
    base_data_dir: str,
    streamer: str,
    vod_filename: str,
    log_path: str,
    force: bool = False
) -> Dict[str, Any]:
    """
    Ensure 10-minute static JSON chunks exist for the given VOD chat log.
    Parses JSONL once and writes chunk_0.json, chunk_1.json, etc.
    """
    chunks_dir = get_chunks_dir(base_data_dir, streamer, vod_filename)
    meta_path = os.path.join(chunks_dir, "meta.json")

    if not force and os.path.isfile(meta_path) and os.path.isfile(log_path):
        try:
            if os.path.getmtime(meta_path) >= os.path.getmtime(log_path):
                with open(meta_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except (OSError, ValueError) as exc:
            logger.warning("chat chunk cache %s unreadable, rebuilding: %s", meta_path, exc)

    if not os.path.isfile(log_path) or os.path.getsize(log_path) == 0:
        return {"ok": False, "error": "Log file not found or empty", "total_chunks": 0}

    os.makedirs(chunks_dir, exist_ok=True)

    chunks: Dict[int, List[Dict[str, Any]]] = {}
    total_messages = 0

    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue

                rel = data.get("rel")
                if rel is None:
                    continue
                try:
                    rel_sec = float(rel)
                except (TypeError, ValueError):
                    continue

                if rel_sec < 0:
                    continue

                chunk_idx = int(rel_sec // CHUNK_DURATION_SEC)
                if chunk_idx not in chunks:
                    chunks[chunk_idx] = []
                chunks[chunk_idx].append(data)
                total_messages += 1

        # Write out each chunk as compact static JSON
        max_chunk = max(chunks.keys()) if chunks else -1
        total_chunks = max_chunk + 1

        for c_idx in range(total_chunks):
            chunk_msgs = chunks.get(c_idx, [])
            c_file = os.path.join(chunks_dir, f"chunk_{c_idx}.json")
            with open(c_file, "w", encoding="utf-8") as out_f:
                json.dump(chunk_msgs, out_f, separators=(",", ":"))

        meta = {
            "ok": True,
            "streamer": streamer,
            "filename": vod_filename,
            "chunk_duration": CHUNK_DURATION_SEC,
            "total_chunks": total_chunks,
            "total_messages": total_messages,
        }
        with open(meta_path, "w", encoding="utf-8") as meta_f:
            json.dump(meta, meta_f)

        logger.info(
            f"Generated {total_chunks} chat chunks ({total_messages} msgs) for {streamer}/{vod_filename}"
        )
        return meta
    except Exception as exc:
        logger.error(f"Failed to generate chat chunks for {streamer}/{vod_filename}: {exc}")
        return {"ok": False, "error": str(exc), "total_chunks": 0}


def get_chunk_file_path(
    base_data_dir: str,
    streamer: str,
    vod_filename: str,
    chunk_idx: int,
    log_path: Optional[str] = None
) -> Optional[str]:
    """Get the absolute path to a specific chunk, generating chunks if missing."""
    chunks_dir = get_chunks_dir(base_data_dir, streamer, vod_filename)
    chunk_file = os.path.join(chunks_dir, f"chunk_{chunk_idx}.json")

    if os.path.isfile(chunk_file):
        return chunk_file

    if log_path and os.path.isfile(log_path):
        meta = ensure_chat_chunks(base_data_dir, streamer, vod_filename, log_path)
        if meta.get("ok") and os.path.isfile(chunk_file):
            return chunk_file

    return None
