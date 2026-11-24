#!/usr/bin/env python3
"""VOD retention: report or delete archive VODs older than N days.

Safe by default: without ``--enforce`` the script only prints what it would
delete (dry run). Deletion goes through the same code path as deleting a VOD
in the WebUI (``WEBUI.webui.delete_video_file``): the VOD gets a
``deleted_media`` tombstone (so cloud sync does not bring it back), and its
thumbnail, HLS cache, chapters file and share links are removed. In addition
the per-VOD sidecars that only belong to that file are removed (metadata JSON,
chat log, chat chunks).

Never touched:
* archive folders with an active recording or a fresh remux lock (the worker
  guards ``_folder_has_active_recording`` / ``_folder_has_active_remux``);
  such a folder is skipped for this run and retried on the next one,
* the file an active recording points to (``.current_recording*.json``),
* folders with an unreadable ``.current_recording*.json`` (treated as busy),
* files with a ``.part`` or ``.remux.lock`` sibling, and ``.part`` / remux
  temporaries themselves,
* VODs somebody is watching, VODs a pending or running cloud task still needs
  and, with cloud auto-upload on, VODs without a confirmed remote copy,
* clips (``<archive>/clips``) and everything outside the direct archive
  folders of the base directory. A linked folder is only an alias of the
  archive folder it resolves to; links to any other place are refused, and so
  are the configured files, metadata, thumbnail, HLS and cache folders.

The tombstone is written before the file is removed and taken back when the
file stays. A database error fails that VOD only: it is kept, the run goes on
with the next one and ends with exit code 1. Size and modification time are
compared with the scan right before the file is removed.

If another media file with the same stem stays (``x.mkv`` expires while
``x.mp4`` is kept, or the other way round), only the expired file itself is
removed, with a tombstone, share links and chapters for exactly its file name:
chat log, chat chunks, metadata, thumbnail, HLS cache and the chat tombstone
are keyed by the stem and still belong to the remaining file.

Extensions are matched case-sensitively like the WebUI resolver: ``X.MP4`` or
``x.MKV`` are never served as VODs and are skipped with a warning.

Offload channels (``--offload-slugs`` / ``VOD_RETENTION_OFFLOAD_SLUGS``): their
VODs are copied elsewhere by an external job. A VOD of such a channel is only
deleted when it is listed in the offload manifest, one ``<channel>/<file>``
entry per line (for example ``somechannel/somechannel_2024-01-01.mp4``),
written by the offload job after a verified copy. The channel part is compared
like the slugs (case-insensitive, trailing ``_`` ignored). Entries without a
channel are ignored with a warning, so a file of one offload channel never
releases a same-named file of another. Without a manifest nothing of these
channels is deleted. Slugs are folder names (letters, digits, ``_``, ``-``,
``.``); other characters are an error, and so is, with ``--enforce``, a slug
without an archive folder (a typo would lift the protection).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Iterator, NamedTuple

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

logger = logging.getLogger("vod_retention")

DEFAULT_RETENTION_DAYS = 90
OFFLOAD_MANIFEST_NAME = "offload_manifest.txt"
# BASE_DIR may be the application directory; these are never archive folders.
_EXTRA_SKIP_DIRS = frozenset({"venv", "node_modules", "static", "templates"})
_MEDIA_EXTENSIONS = (".mp4", ".mkv")
# Any media file sharing a stem keeps the stem's sidecars alive.
_SIBLING_MEDIA_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".ts", ".webm", ".flv", ".mov", ".m4v", ".avi",
})
_DAY_SEC = 86400.0
_OFFLOAD_SLUG_RE = re.compile(r"[a-z0-9_][a-z0-9_.-]*")


class Candidate(NamedTuple):
    path: Path
    slug: str
    size: int
    age_days: float
    mtime_ns: int


class ArchiveDir(NamedTuple):
    """An archive folder and every entry name (folder, links) that leads to it."""

    path: Path
    names: frozenset[str]


class ScanResult(NamedTuple):
    candidates: list[Candidate]
    held_back: int
    busy_dirs: list[str]
    kept: list[tuple[str, str]]
    failed: list[tuple[str, str]]


def _config() -> ModuleType:
    from core import config

    return config


def _worker() -> ModuleType:
    from recorder import worker

    return worker


def _webui() -> ModuleType:
    """Import the WebUI module without its import-time bootstrap."""
    if "WEBUI.webui" in sys.modules:
        return sys.modules["WEBUI.webui"]
    previous = os.environ.get("STREAMVAULT_IMPORT_ONLY")
    os.environ["STREAMVAULT_IMPORT_ONLY"] = "1"
    try:
        from WEBUI import webui
    finally:
        if previous is None:
            os.environ.pop("STREAMVAULT_IMPORT_ONLY", None)
        else:
            os.environ["STREAMVAULT_IMPORT_ONLY"] = previous
    return webui


def format_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{num_bytes} B"


def is_archive_media_filename(filename: str) -> bool:
    """True for final archive VOD names (mp4/mkv, no part/remux temporaries).

    Case-sensitive like ``WEBUI.webui.resolve_media_path``: ``X.MP4`` is no
    archive VOD (see ``has_foreign_extension_case``).
    """
    from core.mp4_utils import is_archive_mp4_filename, is_transient_media_filename

    name = os.path.basename(str(filename or ""))
    if not name or name.startswith(".") or is_transient_media_filename(name):
        return False
    if name.endswith(".mp4"):
        return is_archive_mp4_filename(name)
    return name.endswith(".mkv")


def has_foreign_extension_case(filename: str) -> bool:
    """True for media names like ``X.MP4`` / ``x.Mkv`` that the WebUI ignores."""
    from core.mp4_utils import is_transient_media_filename

    name = os.path.basename(str(filename or ""))
    if not name or name.startswith(".") or is_transient_media_filename(name):
        return False
    lower = name.lower()
    return any(lower.endswith(ext) and not name.endswith(ext) for ext in _MEDIA_EXTENSIONS)


def parse_offload_slugs(raw: str | None) -> frozenset[str]:
    """'Foo_, bar' -> {'foo', 'bar'} (lowercase, trailing '_' stripped).

    Raises ValueError for entries that cannot be a folder name ('a b', 'a;b'):
    dropping or misreading one would lift the protection of that channel.
    """
    slugs = set()
    for part in str(raw or "").split(","):
        entry = part.strip()
        if not entry:
            continue
        slug = _normalize_slug(entry)
        if not slug or not _OFFLOAD_SLUG_RE.fullmatch(slug):
            raise ValueError(f"invalid offload slug {entry!r}")
        slugs.add(slug)
    return frozenset(slugs)


def _normalize_slug(name: str) -> str:
    return str(name or "").strip().lower().rstrip("_")


def is_offload_slug(dir_name: str, offload_slugs: frozenset[str]) -> bool:
    return _normalize_slug(dir_name) in offload_slugs


def manifest_key(slug: str, filename: str) -> str:
    """Manifest entry for a VOD: ``<normalized channel>/<file name>``."""
    return f"{_normalize_slug(slug)}/{filename}"


def load_offload_manifest(path: Path | None) -> set[str] | None:
    """``<channel>/<file>`` keys of verified offloaded VODs (see ``manifest_key``).

    None when there is no manifest. Entries without a channel part (a bare
    file name) or with more than one path separator are ignored with a
    warning: they cannot tell same-named files of two channels apart.
    """
    if path is None:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        logger.warning("Offload manifest missing: %s (offload channels are kept)", path)
        return None
    except (OSError, UnicodeError) as exc:
        logger.error("Offload manifest unreadable: %s (%s); offload channels are kept", path, exc)
        return None
    keys: set[str] = set()
    ignored = 0
    for number, line in enumerate(text.splitlines(), start=1):
        entry = line.strip().replace("\\", "/")
        if not entry or entry.startswith("#"):
            continue
        parts = entry.split("/")
        if len(parts) != 2 or not _normalize_slug(parts[0]) or not parts[1].strip():
            ignored += 1
            logger.warning("Offload manifest %s line %d ignored: %r is not <channel>/<file>",
                           path, number, entry)
            continue
        keys.add(manifest_key(parts[0], parts[1].strip()))
    if ignored:
        logger.warning("Offload manifest %s: %d entries without <channel>/<file> ignored",
                       path, ignored)
    return keys


def _media_stem(name: str) -> str:
    lower = name.lower()
    for ext in _MEDIA_EXTENSIONS:
        if lower.endswith(ext):
            return name[: -len(ext)]
    return name


def _is_recording_pointer(name: str) -> bool:
    return name == ".current_recording.json" or (
        name.startswith(".current_recording.") and name.endswith(".json")
    )


def protected_stems(archive_dir: Path) -> set[str]:
    """Stems of files an active recording or remux may still use.

    Stems instead of names: Twitch copy recordings store ``.mp4`` in the
    pointer while writing ``.mkv.part``.
    """
    stems: set[str] = set()
    try:
        names = [entry.name for entry in archive_dir.iterdir()]
    except OSError as exc:
        logger.warning("Cannot list %s: %s", archive_dir, exc)
        return stems
    for name in names:
        if _is_recording_pointer(name):
            pointer = archive_dir / name
            try:
                data = json.loads(pointer.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError) as exc:
                logger.warning("Recording pointer %s unreadable: %s", pointer, exc)
                continue
            filename = data.get("filename") if isinstance(data, dict) else None
            if filename:
                stems.add(_media_stem(os.path.basename(str(filename))))
        elif name.endswith(".part"):
            stems.add(_media_stem(name[: -len(".part")]))
        elif name.endswith(".remux.lock"):
            stems.add(_media_stem(name[: -len(".remux.lock")]))
    return stems


def has_unreadable_recording_pointer(archive_dir: Path) -> bool:
    """True when a recording pointer exists but does not say which file it means."""
    try:
        names = [entry.name for entry in archive_dir.iterdir()]
    except OSError as exc:
        logger.warning("Cannot list %s: %s", archive_dir, exc)
        return True
    for name in names:
        if not _is_recording_pointer(name):
            continue
        pointer = archive_dir / name
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.info("Recording pointer %s vanished while reading", pointer)
            continue
        except (OSError, UnicodeError, ValueError) as exc:
            logger.warning("Recording pointer %s unreadable, folder kept: %s", pointer, exc)
            return True
        if not isinstance(data, dict):
            logger.warning("Recording pointer %s is no JSON object, folder kept", pointer)
            return True
    return False


def folder_is_busy(archive_dir: Path) -> bool:
    """Worker guards (active recording, fresh remux lock) or unreadable pointer."""
    worker = _worker()
    folder = str(archive_dir)
    return bool(
        worker._folder_has_active_recording(folder)
        or worker._folder_has_active_remux(folder)
        or has_unreadable_recording_pointer(archive_dir)
    )


def _real_key(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def configured_data_dirs(base_dir: Path) -> dict[str, str]:
    """Real paths of configured folders that are never archives: {path: setting}."""
    config = _config()
    paths = {
        "VOD_FILES_DIR": config.FILES_DIR,
        "VOD_META_DIR": config.META_DIR,
        "VOD_THUMB_DIR": config.THUMB_ROOT,
        "VOD_HLS_DIR": config.HLS_ROOT,
        "LIVE_HLS_DIR": config.LIVE_HLS_ROOT,
        "VOD cache": os.path.join(base_dir, ".vodcache"),
    }
    return {_real_key(path): setting for setting, path in paths.items() if path}


def _is_skipped_name(name: str) -> bool:
    return _config().is_reserved_archive_name(name) or name.lower() in _EXTRA_SKIP_DIRS


def iter_archive_dirs(base_dir: Path) -> Iterator[ArchiveDir]:
    """Archive folders of base_dir, each real folder once.

    Linked entries (symlinks, junctions) only add a name to the archive folder
    they resolve to. Links that leave base_dir or do not end at one of its
    direct folders are refused.
    """
    try:
        entries = sorted(base_dir.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        logger.error("Cannot list %s: %s", base_dir, exc)
        return
    base_key = _real_key(base_dir)
    data_dirs = configured_data_dirs(base_dir)
    found: dict[str, set[str]] = {}
    real_names: dict[str, str] = {}
    for entry in entries:
        if _is_skipped_name(entry.name) or not entry.is_dir():
            continue
        real = os.path.realpath(entry)
        key = os.path.normcase(real)
        if os.path.normcase(os.path.dirname(real)) != base_key:
            logger.warning("Skipping %s: links to %s, which is no archive folder of %s",
                           entry.name, real, base_dir)
            continue
        if key in data_dirs:
            logger.warning("Skipping %s: configured as %s", entry.name, data_dirs[key])
            continue
        real_name = os.path.basename(real)
        if _is_skipped_name(real_name):
            logger.warning("Skipping %s: links to the reserved folder %s", entry.name, real_name)
            continue
        found.setdefault(key, set()).update({entry.name, real_name})
        real_names[key] = real_name
    for key, names in found.items():
        yield ArchiveDir(base_dir / real_names[key], frozenset(names))


def unmatched_offload_slugs(base_dir: Path, offload_slugs: frozenset[str]) -> list[str]:
    """Offload slugs without any archive folder (most likely a typo)."""
    if not offload_slugs:
        return []
    try:
        known = {_normalize_slug(p.name) for p in base_dir.iterdir()
                 if p.is_dir() and not _is_skipped_name(p.name)}
    except OSError as exc:
        logger.error("Cannot list %s: %s", base_dir, exc)
        known = set()
    return sorted(offload_slugs - known)


def _state_checks_available(base_dir: Path) -> bool:
    """False when the configured database does not describe base_dir (foreign dry run)."""
    config = _config()
    if _real_key(base_dir) != _real_key(config.BASE_DIR):
        logger.warning("%s is not VOD_BASE_DIR: cloud uploads and playback are not checked", base_dir)
        return False
    env_base = os.environ.get("VOD_BASE_DIR")
    db_file = os.path.join(env_base, "streamrecorder.db") if env_base else config.DB_FILE
    if not os.path.isfile(db_file):
        logger.warning("Database %s missing: cloud uploads and playback are not checked", db_file)
        return False
    return True


def hold_reason(slug: str, path: Path, *, verify: bool | None = None) -> str:
    """Why this VOD must stay ("" = may go): playback or a missing cloud copy."""
    from core import vod_watch_slots
    from core.storage import service

    names = {name.lower() for name in service.media_aliases(slug)}
    if any(streamer in names and filename == path.name.lower()
           for streamer, filename in vod_watch_slots.list_watched_vods()):
        return "being watched"
    return service.local_delete_block_reason(str(path), verify=verify)


def _candidate_errors() -> tuple[type[BaseException], ...]:
    """Errors that fail one VOD but not the run."""
    from core import database
    from core.storage.base import StorageError

    return (*database.DB_ACCESS_ERRORS, StorageError)


def find_expired_archives(
    base_dir: Path,
    retention_days: int,
    now: float | None = None,
    *,
    offload_slugs: frozenset[str] = frozenset(),
    offload_manifest: set[str] | None = None,
) -> ScanResult:
    """Collect expired archive VODs that are safe to delete."""
    current = time.time() if now is None else now
    cutoff = current - retention_days * _DAY_SEC
    candidates: list[Candidate] = []
    held_back = 0
    busy_dirs: list[str] = []
    kept: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    state_checks: bool | None = None
    for archive_dir, dir_names in iter_archive_dirs(base_dir):
        if folder_is_busy(archive_dir):
            logger.warning("Skipping %s: active recording or remux", archive_dir.name)
            busy_dirs.append(archive_dir.name)
            continue
        protected = protected_stems(archive_dir)
        root = archive_dir.resolve()
        # A linked folder protects the files it shows, whatever name is listed.
        offload = any(is_offload_slug(name, offload_slugs) for name in dir_names)
        try:
            entries = sorted(archive_dir.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            logger.warning("Cannot list %s: %s", archive_dir, exc)
            continue
        for entry in entries:
            name = entry.name
            if has_foreign_extension_case(name):
                logger.warning("Skipping %s/%s: extension case differs from .mp4/.mkv "
                               "(the WebUI does not serve it)", archive_dir.name, name)
                continue
            if not is_archive_media_filename(name) or _media_stem(name) in protected:
                continue
            if entry.is_symlink() or not entry.is_file():
                continue
            try:
                st = entry.stat()
                inside = entry.resolve().is_relative_to(root)
            except OSError as exc:
                logger.warning("Cannot stat %s: %s", entry, exc)
                continue
            if not inside or st.st_mtime >= cutoff:
                continue
            if offload and (
                offload_manifest is None
                or not any(manifest_key(alias, name) in offload_manifest for alias in dir_names)
            ):
                held_back += 1
                continue
            if state_checks is None:
                state_checks = _state_checks_available(base_dir)
            if state_checks:
                label = f"{archive_dir.name}/{name}"
                try:
                    # The provider itself is only asked right before deletion.
                    reason = hold_reason(archive_dir.name, entry, verify=False)
                except _candidate_errors() as exc:
                    logger.error("Cannot check %s, kept: %s", entry, exc)
                    failed.append((label, str(exc)))
                    continue
                if reason:
                    logger.info("Kept %s: %s", entry, reason)
                    kept.append((label, reason))
                    continue
            age_days = (current - st.st_mtime) / _DAY_SEC
            candidates.append(
                Candidate(entry, archive_dir.name, int(st.st_size), age_days, st.st_mtime_ns))
    if held_back:
        logger.warning("%d expired VOD(s) of offload channels kept (not in manifest)", held_back)
    return ScanResult(candidates, held_back, busy_dirs, kept, failed)


def _sidecars(webui: ModuleType, path: Path) -> list[Path]:
    """Files that only belong to this VOD (metadata, transcript, chat log, chat chunks)."""
    from core import chat_chunker

    paths = [Path(p) for p in webui.sidecar_metadata_candidates(str(path))]
    paths.append(path.parent / f"{_media_stem(path.name)}.transcript.txt")
    paths.append(path.parent / webui.CHAT_LOG_SUBDIR / f"{_media_stem(path.name)}.jsonl")
    paths.append(Path(chat_chunker.get_chunks_dir(str(path.parent.parent), path.parent.name, path.name)))
    return paths


def other_media_with_stem(path: Path) -> list[Path]:
    """Other media files in the same folder that share ``path``'s stem.

    Raises OSError when the folder cannot be listed; the caller then counts the
    VOD as failed instead of guessing.
    """
    stem = _media_stem(path.name)
    found = []
    for sibling in path.parent.iterdir():
        if sibling.name == path.name:
            continue
        base, ext = os.path.splitext(sibling.name)
        if base == stem and ext.lower() in _SIBLING_MEDIA_EXTENSIONS:
            found.append(sibling)
    return found


def _remove_below(path: Path, root: Path) -> None:
    """Delete a file or directory below root; refuses symlinks and escapes."""
    if path.is_symlink():
        raise OSError(f"refusing to delete symlink {path}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise OSError(f"{path} is outside {root}")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _write_tombstones(webui: ModuleType, slug: str, filenames: list[str],
                      written: list[tuple[str, str]]) -> None:
    """Tombstones under every slug the WebUI looks up; new ones go to ``written``."""
    from core import database

    slugs = {slug, webui.archive_name_for(slug) or slug, *webui.related_archive_slugs(slug)}
    for name in sorted(s for s in slugs if s):
        for filename in filenames:
            known = database.is_deleted_media(streamer=name, filename=filename)
            if not database.upsert_deleted_media(name, filename, file_id=None, deleted_by=None):
                raise ValueError(f"no tombstone possible for {name}/{filename}")
            if not known:
                written.append((name, filename))


def _take_back_tombstones(written: list[tuple[str, str]]) -> None:
    from core import database

    for name, filename in written:
        try:
            database.remove_deleted_media([name], filename)
        except database.DB_ACCESS_ERRORS as exc:
            logger.error("Tombstone of kept file %s/%s not taken back (hidden in the WebUI): %s",
                         name, filename, exc)


def _forget_single_file(webui: ModuleType, slug: str, path: Path, archive_dir: Path) -> None:
    """File-name-keyed cleanup for a VOD whose stem lives on in another file.

    Removes its share links and chapters file and clears the WebUI listing
    caches. Stem-keyed files stay untouched.
    """
    filename = path.name
    webui.remove_shares_for(slug, filename, "vod")
    chapters = webui.chapter_path(slug, filename)
    if chapters:
        chap = Path(chapters)
        if chap.exists() or chap.is_symlink():
            try:
                _remove_below(chap, archive_dir)
            except OSError as exc:
                logger.error("Chapters %s of %s not removed: %s", chap, filename, exc)
    webui._VOD_LIST_CACHE.pop(slug, None)
    webui.STREAMER_CACHE["ts"] = 0
    webui.bust_media_cache()
    try:
        webui.invalidate_dashboard_media_cache()
    except OSError as exc:
        logger.warning("Dashboard cache not invalidated after deleting %s: %s", filename, exc)


def _changed_since_scan(candidate: Candidate) -> str:
    """Why the candidate must stay after all ("" = unchanged)."""
    path = candidate.path
    if folder_is_busy(path.parent) or _media_stem(path.name) in protected_stems(path.parent):
        return "recording or remux started meanwhile"
    try:
        st = path.lstat()
    except FileNotFoundError:
        return "file is gone"
    if not stat.S_ISREG(st.st_mode) or (
        (int(st.st_size), st.st_mtime_ns) != (candidate.size, candidate.mtime_ns)
    ):
        return "file changed since the scan"
    return ""


def delete_candidate(candidate: Candidate) -> str:
    """Delete one VOD like the WebUI does, plus its sidecars.

    Returns "deleted", "kept" (no longer safe to delete, see log) or "failed".
    The tombstone is written first: without it, nothing is deleted. It is taken
    back when the file stays.
    """
    from core import database

    webui = _webui()
    path = candidate.path
    archive_dir = path.parent
    reason = _changed_since_scan(candidate) or hold_reason(candidate.slug, path)
    if reason:
        logger.warning("Kept %s: %s", path, reason)
        return "kept"
    siblings = other_media_with_stem(path)
    if not siblings and path.name.endswith(".mp4"):
        # delete_video_file resolves the file itself (archive aliases first);
        # only hand it over when it resolves to exactly this file.
        resolved = webui.resolve_media_path(candidate.slug, path.name, "vod")
        if not resolved or not os.path.samefile(resolved, path):
            logger.error("Not deleted %s: WebUI resolves it to %s", path, resolved)
            return "failed"
    # With siblings the chat log (and its tombstone) belongs to the kept file.
    filenames = [path.name] if siblings else [path.name, f"{_media_stem(path.name)}.jsonl"]
    written: list[tuple[str, str]] = []
    removing = False
    complete = True
    try:
        _write_tombstones(webui, candidate.slug, filenames, written)
        reason = _changed_since_scan(candidate)
        if reason:
            logger.warning("Kept %s: %s", path, reason)
            return "kept"
        removing = True
        if siblings:
            # Chat log, chunks, metadata, thumbnail and HLS cache are keyed by
            # the stem and still belong to the kept file.
            _remove_below(path, archive_dir)
            _forget_single_file(webui, candidate.slug, path, archive_dir)
            logger.info("Deleted %s only; stem files kept for %s",
                        path, ", ".join(sorted(s.name for s in siblings)))
            return "deleted"
        if not path.name.endswith(".mp4"):
            # The WebUI does not serve .mkv; remove it here, then let
            # delete_video_file clear shares and caches.
            _remove_below(path, archive_dir)
        try:
            if not webui.delete_video_file(candidate.slug, path.name, False):
                logger.error("WebUI delete failed for %s", path)
                return "failed"
        except database.DB_ACCESS_ERRORS as exc:
            if os.path.lexists(path):
                raise
            # The file is gone and has its tombstone; no later run finds it
            # again, so the sidecars go now.
            logger.error("%s removed, but the WebUI cleanup failed: %s", path, exc)
            complete = False
    finally:
        if written and (not removing or os.path.lexists(path)):
            _take_back_tombstones(written)
    if path.exists():
        logger.error("File still present after delete: %s", path)
        return "failed"
    for extra in _sidecars(webui, path):
        if extra.exists() or extra.is_symlink():
            try:
                _remove_below(extra, archive_dir)
            except OSError as exc:
                logger.error("Sidecar %s of %s not removed: %s", extra, path.name, exc)
    return "deleted" if complete else "failed"


def run(
    base_dir: Path,
    retention_days: int,
    enforce: bool,
    *,
    offload_slugs: frozenset[str] = frozenset(),
    offload_manifest: set[str] | None = None,
    now: float | None = None,
) -> int:
    """Report or delete expired VODs. Exit code 0 = ok, 1 = failures, 2 = setup error."""
    base_dir = base_dir.resolve()
    mode = "ENFORCE" if enforce else "DRY-RUN"
    if enforce:
        webui_base = Path(_webui().BASE_DIR).resolve()
        if webui_base != base_dir:
            logger.error("Base dir %s differs from the configured VOD_BASE_DIR %s", base_dir, webui_base)
            return 2
    for slug in unmatched_offload_slugs(base_dir, offload_slugs):
        message = f"Offload slug {slug!r} has no archive folder in {base_dir} (typo?)"
        if enforce:
            logger.error("%s; nothing deleted", message)
            return 2
        logger.warning("%s; --enforce would refuse to run", message)
    scan = find_expired_archives(
        base_dir, retention_days, now,
        offload_slugs=offload_slugs, offload_manifest=offload_manifest,
    )
    total = sum(c.size for c in scan.candidates)
    print(f"[{mode}] base={base_dir} retention_days={retention_days}")
    print(f"[{mode}] candidates={len(scan.candidates)} total={format_bytes(total)} "
          f"offload_kept={scan.held_back} busy_folders={len(scan.busy_dirs)} "
          f"in_use_kept={len(scan.kept)}")
    failures = len(scan.failed)
    for label, error in scan.failed:
        print(f"FAILED        {label}  ({error})")
    for label, reason in scan.kept:
        print(f"KEPT          {label}  ({reason})")
    for cand in scan.candidates:
        line = f"{cand.slug}/{cand.path.name}  {format_bytes(cand.size)}  age={cand.age_days:.1f}d"
        if not enforce:
            print(f"WOULD DELETE  {line}")
            continue
        try:
            outcome = delete_candidate(cand)
        except _candidate_errors() as exc:
            # Never abort the run: the remaining VODs are independent.
            state = "file still present" if os.path.lexists(cand.path) else "file already removed"
            logger.error("Failed to delete %s (%s): %s", cand.path, state, exc)
            outcome = "failed"
        if outcome == "deleted":
            logger.info("Deleted %s", line)
            print(f"DELETED       {line}")
        elif outcome == "kept":
            print(f"KEPT          {line}  (no longer safe to delete, see log)")
        else:
            failures += 1
            print(f"FAILED        {line}")
    for name in scan.busy_dirs:
        print(f"SKIPPED       {name}/  (active recording or remux, retried next run)")
    if not scan.candidates:
        print(f"[{mode}] nothing to delete")
    return 1 if failures else 0


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {raw!r}") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Delete archive VODs older than N days (dry run unless --enforce).")
    parser.add_argument("--days", type=_positive_int, default=None,
                        help=f"Retention in days (env VOD_RETENTION_DAYS, default {DEFAULT_RETENTION_DAYS}).")
    parser.add_argument("--enforce", action="store_true",
                        help="Actually delete. Without it only a report is printed.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only (default; for explicit use in scripts).")
    parser.add_argument("--base-dir", default=None,
                        help="Archive base directory (default VOD_BASE_DIR).")
    parser.add_argument("--offload-slugs", default=None,
                        help="Comma list of offload channels (env VOD_RETENTION_OFFLOAD_SLUGS).")
    parser.add_argument("--offload-manifest", default=None,
                        help="Offload manifest (env VOD_RETENTION_OFFLOAD_MANIFEST, "
                             f"default <VOD_META_DIR>/{OFFLOAD_MANIFEST_NAME}); one "
                             "<channel>/<file> entry per line, bare file names are ignored.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run and args.enforce:
        print("Pass either --dry-run or --enforce, not both.", file=sys.stderr)
        return 2
    if args.base_dir and "core.config" not in sys.modules:
        # Derived paths (database, .meta, .thumbs) follow the base dir.
        os.environ["VOD_BASE_DIR"] = str(Path(args.base_dir).resolve())

    config = _config()
    _worker()
    if args.enforce:
        _webui()
    # recorder.worker reconfigures logging on import; set ours afterwards.
    logging.basicConfig(level=logging.INFO, force=True,
                        format="%(asctime)s - VOD_RETENTION - %(levelname)s - %(message)s")

    retention_days = args.days
    if retention_days is None:
        raw_days = os.environ.get("VOD_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
        try:
            retention_days = _positive_int(raw_days)
        except argparse.ArgumentTypeError as exc:
            logger.error("VOD_RETENTION_DAYS=%r invalid: %s", raw_days, exc)
            return 2

    base_dir = Path(args.base_dir or config.BASE_DIR).resolve()
    if not base_dir.is_dir():
        logger.error("Base dir does not exist: %s", base_dir)
        return 2

    raw_slugs = args.offload_slugs
    if raw_slugs is None:
        raw_slugs = os.environ.get("VOD_RETENTION_OFFLOAD_SLUGS", "")
    try:
        offload_slugs = parse_offload_slugs(raw_slugs)
    except ValueError as exc:
        logger.error("--offload-slugs / VOD_RETENTION_OFFLOAD_SLUGS: %s", exc)
        return 2
    manifest_raw = args.offload_manifest or os.environ.get("VOD_RETENTION_OFFLOAD_MANIFEST", "").strip()
    manifest_path = Path(manifest_raw) if manifest_raw else Path(config.META_DIR) / OFFLOAD_MANIFEST_NAME
    manifest = load_offload_manifest(manifest_path) if offload_slugs else None

    return run(base_dir, retention_days, bool(args.enforce),
               offload_slugs=offload_slugs, offload_manifest=manifest)


if __name__ == "__main__":
    raise SystemExit(main())
