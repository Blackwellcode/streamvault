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
* files with a ``.part`` or ``.remux.lock`` sibling, and ``.part`` / remux
  temporaries themselves,
* clips (``<archive>/clips``) and everything outside the direct archive
  folders of the base directory.

Offload channels (``--offload-slugs`` / ``VOD_RETENTION_OFFLOAD_SLUGS``): their
VODs are copied elsewhere by an external job. A VOD of such a channel is only
deleted when its file name is listed in the offload manifest (one file name
per line, written by the offload job after a verified copy). Without a
manifest nothing of these channels is deleted.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
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
_DAY_SEC = 86400.0


class Candidate(NamedTuple):
    path: Path
    slug: str
    size: int
    age_days: float


class ScanResult(NamedTuple):
    candidates: list[Candidate]
    held_back: int
    busy_dirs: list[str]


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
    """True for final archive VOD names (mp4/mkv, no part/remux temporaries)."""
    from core.mp4_utils import is_archive_mp4_filename, is_transient_media_filename

    name = os.path.basename(str(filename or ""))
    if not name or name.startswith(".") or is_transient_media_filename(name):
        return False
    lower = name.lower()
    if lower.endswith(".mp4"):
        return is_archive_mp4_filename(name)
    return lower.endswith(".mkv")


def parse_offload_slugs(raw: str | None) -> frozenset[str]:
    """'Foo_, bar' -> {'foo', 'bar'} (lowercase, trailing '_' stripped)."""
    slugs = set()
    for part in str(raw or "").split(","):
        slug = part.strip().lower().rstrip("_")
        if slug:
            slugs.add(slug)
    return frozenset(slugs)


def is_offload_slug(dir_name: str, offload_slugs: frozenset[str]) -> bool:
    return str(dir_name or "").strip().lower().rstrip("_") in offload_slugs


def load_offload_manifest(path: Path | None) -> set[str] | None:
    """File names of verified offloaded VODs, or None when there is no manifest."""
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
    names = set()
    for line in text.splitlines():
        entry = line.strip().replace("\\", "/")
        if entry and not entry.startswith("#"):
            names.add(entry.rsplit("/", 1)[-1])
    return names


def _media_stem(name: str) -> str:
    lower = name.lower()
    for ext in _MEDIA_EXTENSIONS:
        if lower.endswith(ext):
            return name[: -len(ext)]
    return name


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
        if name == ".current_recording.json" or (
            name.startswith(".current_recording.") and name.endswith(".json")
        ):
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


def folder_is_busy(archive_dir: Path) -> bool:
    """Worker guards: active recording or fresh remux lock in this folder."""
    worker = _worker()
    folder = str(archive_dir)
    return bool(worker._folder_has_active_recording(folder) or worker._folder_has_active_remux(folder))


def iter_archive_dirs(base_dir: Path) -> Iterator[Path]:
    config = _config()
    try:
        entries = sorted(base_dir.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        logger.error("Cannot list %s: %s", base_dir, exc)
        return
    for entry in entries:
        name = entry.name
        if config.is_reserved_archive_name(name) or name.lower() in _EXTRA_SKIP_DIRS:
            continue
        # The archive folder itself may be a symlink to another drive.
        if entry.is_dir():
            yield entry


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
    for archive_dir in iter_archive_dirs(base_dir):
        if folder_is_busy(archive_dir):
            logger.warning("Skipping %s: active recording or remux", archive_dir.name)
            busy_dirs.append(archive_dir.name)
            continue
        protected = protected_stems(archive_dir)
        root = archive_dir.resolve()
        offload = is_offload_slug(archive_dir.name, offload_slugs)
        try:
            entries = sorted(archive_dir.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            logger.warning("Cannot list %s: %s", archive_dir, exc)
            continue
        for entry in entries:
            name = entry.name
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
            if offload and (offload_manifest is None or name not in offload_manifest):
                held_back += 1
                continue
            age_days = (current - st.st_mtime) / _DAY_SEC
            candidates.append(Candidate(entry, archive_dir.name, int(st.st_size), age_days))
    if held_back:
        logger.warning("%d expired VOD(s) of offload channels kept (not in manifest)", held_back)
    return ScanResult(candidates, held_back, busy_dirs)


def _sidecars(webui: ModuleType, path: Path) -> list[Path]:
    """Files that only belong to this VOD (metadata, chat log, chat chunks)."""
    from core import chat_chunker

    paths = [Path(p) for p in webui.sidecar_metadata_candidates(str(path))]
    paths.append(path.parent / webui.CHAT_LOG_SUBDIR / f"{_media_stem(path.name)}.jsonl")
    paths.append(Path(chat_chunker.get_chunks_dir(str(path.parent.parent), path.parent.name, path.name)))
    return paths


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


def delete_candidate(candidate: Candidate) -> str:
    """Delete one VOD like the WebUI does, plus its sidecars.

    Returns "deleted", "kept" (a recording or remux started meanwhile) or "failed".
    """
    webui = _webui()
    path = candidate.path
    archive_dir = path.parent
    if folder_is_busy(archive_dir) or _media_stem(path.name) in protected_stems(archive_dir):
        logger.warning("Kept %s: recording or remux started meanwhile", path)
        return "kept"
    if path.name.lower().endswith(".mp4"):
        # delete_video_file resolves the file itself (archive aliases first);
        # only hand it over when it resolves to exactly this file.
        resolved = webui.resolve_media_path(candidate.slug, path.name, "vod")
        if not resolved or not os.path.samefile(resolved, path):
            logger.error("Not deleted %s: WebUI resolves it to %s", path, resolved)
            return "failed"
    else:
        # The WebUI does not serve .mkv; remove it here, then let
        # delete_video_file write the tombstone and clear caches.
        _remove_below(path, archive_dir)
    if not webui.delete_video_file(candidate.slug, path.name, False):
        logger.error("WebUI delete failed for %s", path)
        return "failed"
    if path.exists():
        logger.error("File still present after delete: %s", path)
        return "failed"
    for extra in _sidecars(webui, path):
        if extra.exists() or extra.is_symlink():
            try:
                _remove_below(extra, archive_dir)
            except OSError as exc:
                logger.error("Sidecar %s of %s not removed: %s", extra, path.name, exc)
    return "deleted"


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
    scan = find_expired_archives(
        base_dir, retention_days, now,
        offload_slugs=offload_slugs, offload_manifest=offload_manifest,
    )
    total = sum(c.size for c in scan.candidates)
    print(f"[{mode}] base={base_dir} retention_days={retention_days}")
    print(f"[{mode}] candidates={len(scan.candidates)} total={format_bytes(total)} "
          f"offload_kept={scan.held_back} busy_folders={len(scan.busy_dirs)}")
    failures = 0
    for cand in scan.candidates:
        line = f"{cand.slug}/{cand.path.name}  {format_bytes(cand.size)}  age={cand.age_days:.1f}d"
        if not enforce:
            print(f"WOULD DELETE  {line}")
            continue
        try:
            outcome = delete_candidate(cand)
        except OSError as exc:
            logger.error("Failed to delete %s: %s", cand.path, exc)
            outcome = "failed"
        if outcome == "deleted":
            logger.info("Deleted %s", line)
            print(f"DELETED       {line}")
        elif outcome == "kept":
            print(f"KEPT          {line}  (recording or remux started)")
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
                             f"default <VOD_META_DIR>/{OFFLOAD_MANIFEST_NAME}).")
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
    offload_slugs = parse_offload_slugs(raw_slugs)
    manifest_raw = args.offload_manifest or os.environ.get("VOD_RETENTION_OFFLOAD_MANIFEST", "").strip()
    manifest_path = Path(manifest_raw) if manifest_raw else Path(config.META_DIR) / OFFLOAD_MANIFEST_NAME
    manifest = load_offload_manifest(manifest_path) if offload_slugs else None

    return run(base_dir, retention_days, bool(args.enforce),
               offload_slugs=offload_slugs, offload_manifest=manifest)


if __name__ == "__main__":
    raise SystemExit(main())
