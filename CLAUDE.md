# CLAUDE.md — StreamVault (nachtsilo-hub) Engine Guide

## Project Overview
StreamVault is an automated 24/7 multi-platform live stream recording, HLS live restreaming, VOD transcoding, and cloud archival system.
- **Languages & Frameworks:** Python 3.10+ (Ubuntu 22.04 / Debian 12 system Python) (Flask, SQLite WAL, Streamlink, yt-dlp, FFmpeg, Whisper ASR, Boto3, PyCryptodome).
- **Core Services:**
  - `Streamrecorder.py`: Daemon managing per-channel recording processes (`flock`-isolated).
  - `worker.py`: Multi-threaded background job runner (IO, Remux, ASR, Cloud Storage).
  - `WEBUI/webui.py`: Flask dashboard, X-Accel-Redirect streaming, user traffic monitoring, supervisor health checks.
  - `run_transcribe.py`: Isolated subprocess runner for Whisper ASR (`STREAMVAULT_IMPORT_ONLY=1`).
  - `core/storage/`: Provider-agnostic cloud storage service (AES-GCM encryption; S3, WebDAV, Google Drive, OneDrive, Dropbox).
  - `bin/streamvault`: Linux CLI for service orchestration (`start`, `stop`, `restart`, `status`, `logs`, `storage`).

---

## Essential Commands

### Environment Setup
```bash
# Linux
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Windows (Audit Host)
.\.venv\Scripts\activate
pip install -r requirements.txt
```

### Running Tests
The test suite uses Python's standard `unittest` discovery:
```bash
# Run entire test suite (all 167 tests)
python -m unittest discover -s tests -p "test_*.py" -v

# Run specific test modules
python -m unittest tests/test_recorder_reliability.py -v
python -m unittest tests/test_storage.py -v

# Run single test class or test method
python -m unittest tests.test_recorder_reliability.DiskSpaceGuardTests -v
python -m unittest tests.test_recorder_reliability.DiskSpaceGuardTests.test_disk_blocks_recording_on_nonexistent_dir_when_disk_full -v
```

### Syntax & Compilation Verification
Always verify Python syntax before committing:
```bash
# Lint (config in pyproject.toml; CI runs the same check)
ruff check .

# Single file
python -m py_compile recorder/Streamrecorder.py

# Complete repository
python -c "import os, py_compile; [py_compile.compile(os.path.join(r, f), doraise=True) for r, _, fs in os.walk('.') if not any(x in r for x in ('.venv', '__pycache__', '.git')) for f in fs if f.endswith('.py')]"
```

### Service Orchestration
```bash
# Linux CLI
streamvault status
streamvault start
streamvault stop
streamvault restart [webui|recorder|worker]
streamvault logs [webui|recorder|worker] -f
streamvault storage sync

# Direct CLI / Manual Invocation
python recorder/Streamrecorder.py
python recorder/worker.py
python -m WEBUI.webui
```

---

## Architectural Map & Directory Structure

```
nachtsilo-hub/
├── bin/
│   └── streamvault             # Global bash CLI wrapper (/usr/local/bin/streamvault)
├── core/
│   ├── config.py               # Env parsing, default directories (BASE_DIR, HLS_ROOT, etc.)
│   ├── database.py             # SQLite WAL access, schema init, @db_retry, task queues
│   ├── health.py               # Lightweight supervisorctl status parser (no heavy imports)
│   ├── redis_client.py         # Optional high-speed task queue & pub/sub fallback
│   ├── mp4_utils.py            # Fast moov atom shifting & faststart verification
│   └── storage/                # Cloud storage subsystem
│       ├── base.py             # Abstract BaseStorageProvider, mask_error, path normalization
│       ├── crypto.py           # AES-GCM credential encryption (STORAGE_ENCRYPTION_KEY)
│       ├── http.py             # Resilient HTTP session, backoff, sanitized error logs
│       ├── registry.py         # Provider registration & runtime dispatch
│       ├── service.py          # Scheduled background sync, retention pruning, task handlers
│       └── providers/          # S3, WebDAV, Google Drive, OneDrive, Dropbox implementations
├── recorder/
│   ├── Streamrecorder.py       # Multi-channel recording engine, flock locking, FFmpeg pipelines
│   ├── worker.py               # Background task consumer (pools: io, remux, asr, storage)
│   ├── clip_engine.py          # Chat-overlay and highlight clipping
│   └── chat_events.py          # Chat event ingestion & IRC listeners
├── WEBUI/
│   ├── webui.py                # Flask application, auth, routes, X-Accel-Redirect streaming
│   ├── templates/              # Jinja2 HTML templates
│   └── static/                 # CSS, JS, alert audio, icons
├── run_transcribe.py           # Subprocess wrapper for Whisper ASR to keep worker memory clean
├── scripts/
│   └── streamvault_vps.sh      # VPS deploy/rollback + Linux validation (supervisor, X-Accel, HLS RAM disk)
└── tests/
    ├── test_recorder_reliability.py  # 74 tests: process reaping, locking, folder safety, health, client IP, regressions
    └── test_storage.py               # 93 tests: crypto, providers, retention, error masking
```

---

## Critical Rules & Architectural Invariants

### 1. Zero Silent Failures (Anti-Bypass Policy)
- **STRICTLY FORBIDDEN:** `except Exception: pass`, `except: pass`, or returning empty values without logging.
- Catch specific error classes (e.g., `sqlite3.OperationalError`, `subprocess.TimeoutExpired`, `OSError`).
- Always log caught unexpected exceptions using `logger.error(...)` or `logger.warning(...)`.

### 1a. Python 3.10 Compatibility
- Production runs on the distro Python (3.10 on Ubuntu 22.04, 3.11 on Debian 12).
- **No PEP 701 f-strings:** never put backslashes (`\n`), comments or the enclosing quote character inside an f-string `{...}` expression. Build the value in a variable first. Python 3.12+ accepts this silently, so the Windows audit host (3.12) will not catch it; `scripts/streamvault_vps.sh validate` compiles with the target interpreter.
- No 3.11+ stdlib APIs (`tomllib`, `datetime.UTC`, `typing.Self`, `ExceptionGroup`, `except*`).

### 2. Immutable Tests Rule
- **NEVER** weaken, comment out, or delete assertions in `tests/` to make a test pass.
- Fix bugs in the application code, not by relaxing tests.
- When adding new features, add corresponding tests in `tests/test_*.py`.

### 3. Process Management & Lock Hierarchy
- **File Lock Ordering:** When stopping a channel process, **always stop the child process before releasing the channel `flock`**:
  ```python
  # CORRECT:
  stop_channel_and_release_lock(recordings_dir, channel, proc)
  # WRONG:
  release_held_channel_lock(recordings_dir, channel)
  stop_channel_process(proc, channel)  # Races with next recorder process!
  ```
- **Bounded Waiting:** Never call indefinite `process.wait()`. Always use `wait_for_process_exit(proc, timeout=...)` with a hard deadline (default 30s) and fallback to `kill()`.
- **No `fuser`:** Never execute `fuser -k -9` or kill parent process groups.

### 4. Database Concurrency & SQLite WAL
- SQLite is configured with WAL mode (`PRAGMA journal_mode=WAL`).
- All write operations that may experience busy/locked errors must use the `@db_retry(max_retries=5, delay=0.1, backoff=2.0)` decorator and `BEGIN IMMEDIATE`.
- In multi-threaded environments (`worker.py`), each thread **must** obtain its own connection via `get_db()`. Never share SQLite connection handles across threads.
- Stale running tasks are claimed using `claim_next_task()` and must respect `max_attempts_by_type` to avoid endless retry loops on permanently crashing jobs.

### 5. Worker & File Safety
- **Directory Protection:** Before deleting or emptying an archive folder, always check:
  - `_folder_has_active_recording(folder_path)`
  - `_folder_has_active_remux(folder_path)`
  If active, raise `TaskDeferred(delay_sec=120)`.
- **In-Progress Files:** Files ending in `.part` or `.remux.lock` that have a fresh modification timestamp (< 10 min) must **never** be deleted or pruned.
- **Disk Space Protection:** `disk_blocks_recording(path)` walks up to the nearest existing parent directory if `path` does not exist yet. New recordings are paused if disk usage reaches `STORAGE_PAUSE_PCT` (default 90%, env `STREAM_RECORDER_STORAGE_PAUSE_PCT`) or free space falls to `STORAGE_PAUSE_FREE_GB` (default 20 GB, env `STREAM_RECORDER_STORAGE_PAUSE_FREE_GB`).

### 6. Memory & Process Isolation (Whisper ASR)
- Never load heavy ML frameworks (Whisper, PyTorch, CTranslate2) directly into the main `worker.py` process.
- All transcription tasks must execute via `run_transcribe.py` in a separate child process.
- `run_transcribe.py` sets `STREAMVAULT_IMPORT_ONLY=1` to prevent import-time side effects (admin creation, background threads) when loading `WEBUI.webui`.

### 7. Security & Cloud Storage Hygiene
- **Credential Storage:** Provider credentials and OAuth refresh tokens must be encrypted with AES-GCM via `core.storage.crypto.encrypt_secret()`.
- **Log Sanitization:** All cloud errors must be masked via `mask_error(exc)` before writing to database or logs to strip URLs, auth tokens, and S3 credentials.
- **Path Traversal Protection:** All client-supplied filesystem paths must be validated with `_is_direct_child(config.BASE_DIR, path)` or `classify_local_path()`.
- **Cookies:** `VOD_SECURE_COOKIES` defaults to `false` in `.env.example` to prevent redirect loops when Nginx listens on plain HTTP port 80. Set to `true` only when HTTPS is configured.
- **Sentry / Telemetry:** PII sending is disabled by default (`SENTRY_SEND_DEFAULT_PII=false`).

---

## Common Handoff & Troubleshooting Notes
- On Windows hosts, `fcntl` is mocked automatically in test suites via shims in `test_recorder_reliability.py`. In production, the system targets Debian/Ubuntu Linux.
- If Redis is unavailable, task queuing automatically falls back to SQLite `tasks` table with zero configuration needed.
- If Supervisor reports degraded status, check `parse_supervisor_status()` in `core/health.py`. Standard monitored services are `webui`, `recorder`, `worker`.
