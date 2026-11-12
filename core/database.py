import json
import logging
import os
import secrets
import sqlite3
import time

logger = logging.getLogger("database")
from functools import wraps
from contextlib import contextmanager

from core import config
from core.config import DB_FILE, LEGACY_CHANNELS_FILE, LEGACY_LIVE_CHANNEL_FILE


def N_(message: str) -> str:
    """Mark a message for extraction; the web layer translates it (no Flask here)."""
    return message


class GroupError(ValueError):
    """Group validation error. ``msgid`` is English source text for gettext."""

    def __init__(self, msgid: str, **params):
        self.msgid = msgid
        self.params = params
        super().__init__(msgid % params if params else msgid)


def db_retry(max_retries: int = 5, delay: float = 0.1, backoff: float = 2.0):
    """Retry decorator for SQLite write transactions when database is locked or busy."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            current_delay = delay
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as exc:
                    err_msg = str(exc).lower()
                    if ("locked" in err_msg or "busy" in err_msg) and attempt < max_retries - 1:
                        time.sleep(current_delay)
                        current_delay *= backoff
                        continue
                    raise
        return wrapper
    return decorator


def get_db():
    env_base = os.environ.get("VOD_BASE_DIR")
    if env_base:
        active_db = os.path.join(env_base, "streamrecorder.db")
    else:
        active_db = getattr(config, "DB_FILE", DB_FILE)
    db_dir = os.path.dirname(active_db)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(active_db, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA cache_size = -64000")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA mmap_size = 268435456")
    return conn


@contextmanager
def db_transaction():
    """Context manager for SQLite operations with guaranteed commit/rollback and connection closing."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def ensure_login_user_attempts_table(cursor) -> None:
    """Failed logins per (account, network); ``net`` is the /64 or IPv4 address."""
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS login_user_attempts "
        "(username TEXT NOT NULL, net TEXT NOT NULL DEFAULT '', ts REAL NOT NULL)"
    )
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(login_user_attempts)").fetchall()}
    if "net" not in columns:
        try:
            cursor.execute("ALTER TABLE login_user_attempts ADD COLUMN net TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError as exc:
            logger.warning("Migration login_user_attempts.net skipped: %s", exc)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_user_attempts ON login_user_attempts(username, ts)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_user_attempts_net "
        "ON login_user_attempts(username, net, ts)"
    )


def init_db():
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("CREATE TABLE IF NOT EXISTS channels (name TEXT PRIMARY KEY, record INTEGER DEFAULT 0, live INTEGER DEFAULT 0, chat INTEGER DEFAULT 0)")

        channel_columns = {row["name"] for row in c.execute("PRAGMA table_info(channels)").fetchall()}
        for column, definition in {
            "chat": "INTEGER DEFAULT 0",
            "platform": "TEXT DEFAULT 'twitch'",
            "source_url": "TEXT DEFAULT ''",
            "archive_name": "TEXT DEFAULT ''",
            "notify": "INTEGER DEFAULT 1",
        }.items():
            if column not in channel_columns:
                try:
                    c.execute(f"ALTER TABLE channels ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError as exc:
                    # Another process may have added it between PRAGMA and ALTER.
                    logger.warning("Migration channels.%s skipped: %s", column, exc)
        c.execute("UPDATE channels SET platform='twitch' WHERE platform IS NULL OR platform=''")
        c.execute("UPDATE channels SET archive_name=name WHERE archive_name IS NULL OR archive_name=''")

        # Migration: role Spalte zu users hinzufuegen falls fehlt (nur bestehende Tabelle;
        # eine neue Tabelle wird weiter unten bereits mit role angelegt)
        has_users = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        try:
            if has_users:
                c.execute("SELECT role FROM users LIMIT 1")
        except sqlite3.OperationalError:
            try:
                c.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'admin'")
            except sqlite3.OperationalError as exc:
                logger.warning("Migration users.role skipped: %s", exc)
        c.execute("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT, role TEXT, active INTEGER DEFAULT 1)")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS user_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT DEFAULT '',
                permissions TEXT NOT NULL,
                is_system INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_groups_name "
            "ON user_groups(name COLLATE NOCASE)"
        )

        now_init = time.time()
        for g_name, g_desc, g_perms, g_sys in [
            (
                "Administrator",
                "Voller Systemzugriff auf alle Bereiche, Einstellungen und Benutzerverwaltung",
                json.dumps({
                    "can_view_streams": True,
                    "can_download_archive": True,
                    "can_upload_videos": True,
                    "can_delete_videos": True,
                    "can_manage_users": True,
                    "access_all_channels": True,
                    "allowed_channels": [],
                }),
                1,
            ),
            (
                "Moderator",
                "Stream- und Archivverwaltung, Upload und Löschung von Videos ohne Gruppenverwaltung",
                json.dumps({
                    "can_view_streams": True,
                    "can_download_archive": True,
                    "can_upload_videos": True,
                    "can_delete_videos": True,
                    "can_manage_users": False,
                    "access_all_channels": True,
                    "allowed_channels": [],
                }),
                0,
            ),
            (
                "Viewer",
                "Standardrolle mit Leserechten auf Streams und Archiv",
                json.dumps({
                    "can_view_streams": True,
                    "can_download_archive": False,
                    "can_upload_videos": False,
                    "can_delete_videos": False,
                    "can_manage_users": False,
                    "access_all_channels": True,
                    "allowed_channels": [],
                }),
                1,
            ),
        ]:
            c.execute(
                """
                INSERT INTO user_groups (name, description, permissions, is_system, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    is_system = excluded.is_system
                """,
                (g_name, g_desc, g_perms, g_sys, now_init, now_init),
            )

        user_columns = {row["name"] for row in c.execute("PRAGMA table_info(users)").fetchall()}
        for column, definition in {
            "discord_id": "TEXT",
            "discord_username": "TEXT",
            "created_via": "TEXT",
            "created_at": "REAL",
            "register_note": "TEXT",
            "rules_accepted_at": "REAL",
            "rules_version": "TEXT",
            "terms_accepted_at": "REAL",
            "terms_version": "TEXT",
            "last_seen_at": "REAL",
            "last_login_at": "REAL",
            "group_id": "INTEGER",
            "email": "TEXT DEFAULT ''",
            "updated_at": "REAL",
            # Random token, replaced on "log out everywhere" and password changes;
            # cookies carry the value they were issued with.
            "session_version": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if column not in user_columns:
                try:
                    c.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError as exc:
                    logger.warning("Migration users.%s skipped: %s", column, exc)

        c.execute(
            """
            UPDATE users
            SET group_id = (SELECT id FROM user_groups WHERE name = 'Administrator' LIMIT 1),
                updated_at = ?
            WHERE (group_id IS NULL OR group_id = 0) AND role = 'admin'
            """,
            (now_init,),
        )
        c.execute(
            """
            UPDATE users
            SET group_id = (SELECT id FROM user_groups WHERE name = 'Viewer' LIMIT 1),
                updated_at = ?
            WHERE (group_id IS NULL OR group_id = 0)
            """,
            (now_init,),
        )
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_discord_id "
            "ON users(discord_id) WHERE discord_id IS NOT NULL AND discord_id != ''"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS terms_acceptances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                discord_id TEXT,
                terms_version TEXT NOT NULL,
                rules_version TEXT,
                accepted_at REAL NOT NULL,
                source TEXT
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_terms_acceptances_accepted_at "
            "ON terms_acceptances(accepted_at DESC)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_terms_acceptances_username "
            "ON terms_acceptances(username COLLATE NOCASE, accepted_at DESC)"
        )
        c.execute("CREATE TABLE IF NOT EXISTS bans (ip TEXT PRIMARY KEY, created_at REAL, expires_at REAL)")
        ban_columns = {row["name"] for row in c.execute("PRAGMA table_info(bans)").fetchall()}
        if "expires_at" not in ban_columns:
            try:
                c.execute("ALTER TABLE bans ADD COLUMN expires_at REAL")
            except sqlite3.OperationalError as exc:
                logger.warning("Migration bans.expires_at skipped: %s", exc)
        c.execute("CREATE TABLE IF NOT EXISTS login_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ip TEXT, username TEXT, success INTEGER, ts REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS login_attempts (ip TEXT NOT NULL, ts REAL NOT NULL)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_ts ON login_attempts(ip, ts)")
        ensure_login_user_attempts_table(c)
        c.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, payload TEXT, status TEXT DEFAULT 'pending', created_at REAL, updated_at REAL)")
        task_columns = {row["name"] for row in c.execute("PRAGMA table_info(tasks)").fetchall()}
        for column, definition in {
            "attempts": "INTEGER DEFAULT 0",
            "error": "TEXT",
            "worker_id": "TEXT",
            "started_at": "REAL",
            "locked_at": "REAL",
        }.items():
            if column not in task_columns:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")

        # Ensure tables have proper constraints for security
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bans_ip ON bans(ip)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status, created_at, id)")

        # Admin-deleted VODs/clips: keep them out of the UI.
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS deleted_media (
                streamer TEXT NOT NULL,
                filename TEXT NOT NULL,
                file_id TEXT,
                deleted_at REAL NOT NULL,
                deleted_by TEXT,
                PRIMARY KEY (streamer, filename)
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_deleted_media_file_id ON deleted_media(file_id)"
        )

        # Member VOD archive allowlists — per role, not per user.
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS vod_access_defaults (
                streamer TEXT PRIMARY KEY
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS user_vod_access (
                username TEXT NOT NULL,
                streamer TEXT NOT NULL,
                PRIMARY KEY (username, streamer)
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_vod_access_username ON user_vod_access(username)"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS role_vod_access (
                role TEXT NOT NULL,
                streamer TEXT NOT NULL,
                PRIMARY KEY (role, streamer)
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_role_vod_access_role ON role_vod_access(role)"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS user_traffic_daily (
                username TEXT NOT NULL,
                day TEXT NOT NULL,
                logins INTEGER NOT NULL DEFAULT 0,
                pages INTEGER NOT NULL DEFAULT 0,
                media INTEGER NOT NULL DEFAULT 0,
                chats INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (username, day)
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_traffic_daily_day ON user_traffic_daily(day)"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS user_traffic_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS vod_watch_slots (
                holder TEXT PRIMARY KEY,
                username TEXT,
                streamer TEXT,
                filename TEXT,
                acquired_at REAL NOT NULL,
                last_seen_at REAL NOT NULL
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_vod_watch_slots_seen ON vod_watch_slots(last_seen_at)"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        # External storage providers. Credentials are AES-GCM encrypted by
        # core.storage.crypto before they reach this table.
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS storage_providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider_type TEXT NOT NULL,
                config TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_check_at REAL,
                last_check_ok INTEGER,
                last_check_message TEXT,
                last_error TEXT
            )
            """
        )
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_storage_providers_name "
            "ON storage_providers(name COLLATE NOCASE)"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS storage_objects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_id INTEGER NOT NULL,
                local_path TEXT NOT NULL,
                remote_path TEXT NOT NULL,
                streamer TEXT DEFAULT '',
                filename TEXT DEFAULT '',
                kind TEXT DEFAULT 'vod',
                status TEXT NOT NULL DEFAULT 'pending',
                local_size INTEGER DEFAULT 0,
                local_mtime REAL DEFAULT 0,
                remote_size INTEGER DEFAULT 0,
                remote_modified TEXT DEFAULT '',
                remote_checksum TEXT DEFAULT '',
                progress REAL DEFAULT 0,
                attempts INTEGER DEFAULT 0,
                last_error TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                uploaded_at REAL,
                local_deleted_at REAL,
                remote_deleted_at REAL,
                FOREIGN KEY(provider_id) REFERENCES storage_providers(id) ON DELETE CASCADE
            )
            """
        )
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_storage_objects_provider_local "
            "ON storage_objects(provider_id, local_path)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_storage_objects_status "
            "ON storage_objects(status, updated_at)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_storage_objects_media "
            "ON storage_objects(streamer COLLATE NOCASE, filename COLLATE NOCASE)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_storage_objects_provider "
            "ON storage_objects(provider_id, status)"
        )
        # Performance & Concurrency Indexes (users, logs, channels, traffic)
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_group_id ON users(group_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_active ON users(active)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_username_nocase ON users(username COLLATE NOCASE)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_login_logs_ts ON login_logs(ts DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_login_logs_username_ts ON login_logs(username, ts DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_login_logs_ip_ts ON login_logs(ip, ts DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_channels_archive_name ON channels(archive_name)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_vod_access_defaults_streamer_nocase ON vod_access_defaults(streamer COLLATE NOCASE)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_user_traffic_daily_username ON user_traffic_daily(username, day)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_vod_watch_slots_holder_nocase ON vod_watch_slots(holder COLLATE NOCASE)")
        _backfill_user_traffic_from_login_logs(c)

        conn.commit()
        migrate_legacy_data(c)
        _seed_vod_access_defaults(c)
        _migrate_role_vod_access(c)
        conn.commit()
    finally:
        conn.close()


_VOD_ACCESS_SEED: tuple[str, ...] = ()
_ROLE_VOD_ACCESS_DEFAULT_ROLE = "member"


def _normalize_vod_access_slug(streamer: str) -> str:
    slug = str(streamer or "").strip().lower().rstrip("_")
    slug = "".join(ch for ch in slug if ch.isalnum() or ch in ("_", "-"))
    return slug[:64]


def _normalize_vod_access_role(role: str) -> str:
    value = str(role or "").strip().lower()
    if value == "user":
        return "viewer"
    return value


def _seed_vod_access_defaults(c):
    row = c.execute("SELECT COUNT(*) AS n FROM vod_access_defaults").fetchone()
    if row and int(row["n"] or 0) > 0:
        return
    for slug in _VOD_ACCESS_SEED:
        c.execute(
            "INSERT OR IGNORE INTO vod_access_defaults (streamer) VALUES (?)",
            (slug,),
        )


def _migrate_role_vod_access(c):
    """Ensure Discord-Member role has a VOD allowlist (from defaults / seed)."""
    role = _ROLE_VOD_ACCESS_DEFAULT_ROLE
    existing = c.execute(
        "SELECT COUNT(*) AS n FROM role_vod_access WHERE role=?",
        (role,),
    ).fetchone()
    if existing and int(existing["n"] or 0) > 0:
        return
    defaults = [
        str(r["streamer"])
        for r in c.execute("SELECT streamer FROM vod_access_defaults ORDER BY streamer").fetchall()
    ]
    if not defaults:
        defaults = list(_VOD_ACCESS_SEED)
    for slug in defaults:
        clean = _normalize_vod_access_slug(slug)
        if not clean:
            continue
        c.execute(
            "INSERT OR IGNORE INTO role_vod_access (role, streamer) VALUES (?, ?)",
            (role, clean),
        )
        c.execute(
            "INSERT OR IGNORE INTO vod_access_defaults (streamer) VALUES (?)",
            (clean,),
        )


def migrate_legacy_data(c):
    if os.path.exists(LEGACY_CHANNELS_FILE):
        try:
            with open(LEGACY_CHANNELS_FILE, 'r') as f:
                data = json.load(f)
            for ch in data:
                c.execute("INSERT OR IGNORE INTO channels (name, record) VALUES (?, 1)", (ch.lower(),))
                c.execute("UPDATE channels SET record=1 WHERE name=?", (ch.lower(),))
            os.rename(LEGACY_CHANNELS_FILE, LEGACY_CHANNELS_FILE + ".migrated")
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            logger.error("Legacy channel migration from %s failed: %s", LEGACY_CHANNELS_FILE, exc)
    if os.path.exists(LEGACY_LIVE_CHANNEL_FILE):
        try:
            with open(LEGACY_LIVE_CHANNEL_FILE, 'r') as f:
                data = json.load(f)
            channels = data if isinstance(data, list) else ([data["channel"]] if isinstance(data, dict) and "channel" in data else [])
            for ch in channels:
                c.execute("INSERT OR IGNORE INTO channels (name, live) VALUES (?, 1)", (str(ch).lower(),))
                c.execute("UPDATE channels SET live=1 WHERE name=?", (str(ch).lower(),))
            os.rename(LEGACY_LIVE_CHANNEL_FILE, LEGACY_LIVE_CHANNEL_FILE + ".migrated")
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            logger.error("Legacy live-channel migration from %s failed: %s", LEGACY_LIVE_CHANNEL_FILE, exc)

def get_channels(mode='record'):
    conn = get_db()
    try:
        if mode == 'record':
            rows = conn.execute("SELECT name FROM channels WHERE record=1").fetchall()
        elif mode == 'live':
            rows = conn.execute("SELECT name FROM channels WHERE live=1").fetchall()
        elif mode == 'chat':
            rows = conn.execute("SELECT name FROM channels WHERE chat=1").fetchall()
        else:
            rows = conn.execute("SELECT name FROM channels").fetchall()
        return [r['name'] for r in rows]
    finally:
        conn.close()

@db_retry(max_retries=5, delay=0.1, backoff=2.0)
def add_task(task_type, payload):
    conn = get_db()
    try:
        now = time.time()
        cur = conn.execute(
            """
            INSERT INTO tasks (type, payload, status, created_at, updated_at, attempts, error, worker_id, started_at, locked_at)
            VALUES (?, ?, 'pending', ?, ?, 0, NULL, NULL, NULL, NULL)
            """,
            (task_type, json.dumps(payload), now, now),
        )
        conn.commit()
        task_id = cur.lastrowid
    finally:
        conn.close()

    try:
        from core import redis_client
        redis_client.enqueue_task(task_type, payload, task_id)
    except ImportError as exc:
        logger.warning("Redis client unavailable, task %s stays SQLite-only: %s", task_id, exc)

    return task_id


def get_system_state(key: str, default=None):
    """Retrieve a JSON-parsed value from Redis cache first, falling back to system_state table."""
    try:
        from core import redis_client
        cached = redis_client.get_state(f"sysstate:{key}")
        if cached is not None:
            return cached
    except ImportError as exc:
        logger.warning("Redis client unavailable for state %s: %s", key, exc)

    conn = get_db()
    try:
        c = conn.cursor()
        row = c.execute("SELECT value FROM system_state WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            val = json.loads(row["value"])
            try:
                from core import redis_client
                redis_client.set_state(f"sysstate:{key}", val, ex=3600)
            except ImportError as exc:
                logger.warning("Redis client unavailable for state %s: %s", key, exc)
            return val
        except json.JSONDecodeError:
            return default
    finally:
        conn.close()


@db_retry(max_retries=5, delay=0.1, backoff=2.0)
def set_system_state(key: str, value):
    """Store a JSON-serializable value in Redis and persist in the system_state table."""
    try:
        from core import redis_client
        redis_client.set_state(f"sysstate:{key}", value)
    except ImportError as exc:
        logger.warning("Redis client unavailable for state %s: %s", key, exc)

    conn = get_db()
    try:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO system_state (key, value, updated_at) 
            VALUES (?, ?, ?) 
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, json.dumps(value), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


@db_retry(max_retries=5, delay=0.1, backoff=2.0)
def claim_task_by_id(task_id, worker_id):
    """Claim a specific task ID directly (e.g. dispatched from Redis queue)."""
    conn = get_db()
    now = time.time()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, type, payload, status, created_at, updated_at, attempts, error, worker_id, started_at, locked_at
            FROM tasks
            WHERE id=? AND status='pending'
            """,
            (task_id,),
        ).fetchone()
        if not row:
            conn.commit()
            return None
        attempts = int(row["attempts"] or 0) + 1
        conn.execute(
            """
            UPDATE tasks
            SET status='running', attempts=?, worker_id=?, started_at=?, locked_at=?, updated_at=?, error=NULL
            WHERE id=? AND status='pending'
            """,
            (attempts, worker_id, now, now, now, row["id"]),
        )
        conn.commit()
        task = dict(row)
        task.update({
            "status": "running",
            "attempts": attempts,
            "worker_id": worker_id,
            "started_at": now,
            "locked_at": now,
            "updated_at": now,
            "error": None,
        })
        return task
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def list_tasks(limit=30):
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT id, type, payload, status, created_at, updated_at, attempts, error, worker_id, started_at, locked_at
            FROM tasks
            ORDER BY updated_at DESC, created_at DESC, id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@db_retry(max_retries=5, delay=0.1, backoff=2.0)
def claim_next_task(worker_id, stale_after=7200, allowed_types=None, max_attempts_by_type=None):
    """Claim the oldest pending task, optionally restricted to allowed_types.

    When allowed_types is set (non-empty sequence), only those task types are
    considered - used by the worker to avoid claiming work for saturated pools.
    When max_attempts_by_type is provided, stale tasks that exhausted their
    retry budget are failed instead of being requeued forever.
    """
    conn = get_db()
    now = time.time()
    cutoff = now - max(60, int(stale_after or 7200))
    type_filter = None
    if allowed_types is not None:
        type_filter = tuple(str(t) for t in allowed_types if t)
        if not type_filter:
            conn.close()
            return None
    try:
        conn.execute("BEGIN IMMEDIATE")
        stale_rows = conn.execute(
            """
            SELECT id, type, attempts
            FROM tasks
            WHERE status='running' AND locked_at IS NOT NULL AND locked_at < ?
            """,
            (cutoff,),
        ).fetchall()
        for stale in stale_rows:
            attempts = int(stale["attempts"] or 0)
            if max_attempts_by_type is None:
                exhausted = False
            else:
                max_attempts = max(
                    1, int(max_attempts_by_type.get(stale["type"], 1) or 1)
                )
                exhausted = attempts >= max_attempts
            if exhausted:
                conn.execute(
                    """
                    UPDATE tasks
                    SET status='failed', worker_id=NULL, locked_at=NULL, updated_at=?,
                        error='Worker timeout; max attempts reached.'
                    WHERE id=? AND status='running'
                    """,
                    (now, stale["id"]),
                )
            else:
                conn.execute(
                    """
                    UPDATE tasks
                    SET status='pending', worker_id=NULL, locked_at=NULL, updated_at=?,
                        error='Worker timeout, neu eingereiht.'
                    WHERE id=? AND status='running'
                    """,
                    (now, stale["id"]),
                )
        if type_filter:
            placeholders = ",".join("?" for _ in type_filter)
            row = conn.execute(
                f"""
                SELECT id, type, payload, status, created_at, updated_at, attempts, error, worker_id, started_at, locked_at
                FROM tasks
                WHERE status='pending' AND type IN ({placeholders}) AND created_at <= ?
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                (*type_filter, now),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT id, type, payload, status, created_at, updated_at, attempts, error, worker_id, started_at, locked_at
                FROM tasks
                WHERE status='pending' AND created_at <= ?
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
        if not row:
            conn.commit()
            return None
        attempts = int(row["attempts"] or 0) + 1
        conn.execute(
            """
            UPDATE tasks
            SET status='running', attempts=?, worker_id=?, started_at=?, locked_at=?, updated_at=?, error=NULL
            WHERE id=? AND status='pending'
            """,
            (attempts, worker_id, now, now, now, row["id"]),
        )
        conn.commit()
        task = dict(row)
        task.update({
            "status": "running",
            "attempts": attempts,
            "worker_id": worker_id,
            "started_at": now,
            "locked_at": now,
            "updated_at": now,
            "error": None,
        })
        return task
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@db_retry(max_retries=5, delay=0.1, backoff=2.0)
def complete_task(task_id, worker_id=None):
    """Mark task completed only if still owned by this worker (or ownership unset)."""
    conn = get_db()
    try:
        now = time.time()
        if worker_id:
            conn.execute(
                """
                UPDATE tasks
                SET status='completed', updated_at=?, worker_id=NULL, locked_at=NULL, error=NULL
                WHERE id=? AND status='running' AND worker_id=?
                """,
                (now, task_id, worker_id),
            )
        else:
            conn.execute(
                """
                UPDATE tasks
                SET status='completed', updated_at=?, worker_id=NULL, locked_at=NULL, error=NULL
                WHERE id=? AND status='running'
                """,
                (now, task_id),
            )
        conn.commit()
    finally:
        conn.close()


def fail_task(task_id, error, max_attempts=1, worker_id=None):
    """Fail or requeue a task; only the owning worker may change a running task."""
    conn = get_db()
    try:
        now = time.time()
        if worker_id:
            row = conn.execute(
                "SELECT attempts, status, worker_id FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if not row:
                return "missing"
            if str(row["status"] or "") == "running" and str(row["worker_id"] or "") != str(worker_id):
                return "stale"
        else:
            row = conn.execute("SELECT attempts FROM tasks WHERE id=?", (task_id,)).fetchone()
        attempts = int(row["attempts"] or 0) if row else 0
        final_status = "failed" if attempts >= max(1, int(max_attempts or 1)) else "pending"
        if worker_id:
            conn.execute(
                """
                UPDATE tasks
                SET status=?, updated_at=?, worker_id=NULL, locked_at=NULL, error=?
                WHERE id=? AND status='running' AND worker_id=?
                """,
                (final_status, now, str(error or "")[:500], task_id, worker_id),
            )
        else:
            conn.execute(
                """
                UPDATE tasks
                SET status=?, updated_at=?, worker_id=NULL, locked_at=NULL, error=?
                WHERE id=?
                """,
                (final_status, now, str(error or "")[:500], task_id),
            )
        conn.commit()
        return final_status
    finally:
        conn.close()


def defer_task(task_id, error, delay_sec=60, worker_id=None):
    """Requeue a running task after delay without consuming an attempt.

    Uses created_at in the future so claim_next_task skips it until then.
    """
    conn = get_db()
    try:
        now = time.time()
        delay = max(1, int(delay_sec or 60))
        available_at = now + delay
        err = str(error or "")[:500]
        if worker_id:
            row = conn.execute(
                "SELECT attempts, status, worker_id FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if not row:
                return "missing"
            if str(row["status"] or "") == "running" and str(row["worker_id"] or "") != str(worker_id):
                return "stale"
            attempts = max(0, int(row["attempts"] or 0) - 1)
            cur = conn.execute(
                """
                UPDATE tasks
                SET status='pending', attempts=?, created_at=?, updated_at=?,
                    worker_id=NULL, locked_at=NULL, started_at=NULL, error=?
                WHERE id=? AND status='running' AND worker_id=?
                """,
                (attempts, available_at, available_at, err, task_id, worker_id),
            )
        else:
            row = conn.execute("SELECT attempts FROM tasks WHERE id=?", (task_id,)).fetchone()
            attempts = max(0, int(row["attempts"] or 0) - 1) if row else 0
            cur = conn.execute(
                """
                UPDATE tasks
                SET status='pending', attempts=?, created_at=?, updated_at=?,
                    worker_id=NULL, locked_at=NULL, started_at=NULL, error=?
                WHERE id=?
                """,
                (attempts, available_at, available_at, err, task_id),
            )
        conn.commit()
        changed = cur.rowcount if cur is not None else 0
        return "pending" if changed else "stale"
    finally:
        conn.close()

def _normalize_channel_row(row):
    data = dict(row)
    data["platform"] = str(data.get("platform") or "twitch").strip().lower() or "twitch"
    data["source_url"] = str(data.get("source_url") or "").strip()
    data["archive_name"] = str(data.get("archive_name") or data.get("name") or "").strip()
    data["record"] = bool(int(data.get("record") or 0))
    data["live"] = bool(int(data.get("live") or 0))
    data["notify"] = bool(int(data.get("notify", 1) or 0))
    return data


def get_channel_config(name):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM channels WHERE name=?", (name,)).fetchone()
        if row:
            return _normalize_channel_row(row)
        return None
    finally:
        conn.close()


def list_channel_configs(mode=None):
    conn = get_db()
    try:
        if mode == "record":
            rows = conn.execute("SELECT * FROM channels WHERE record=1 ORDER BY name").fetchall()
        elif mode == "live":
            rows = conn.execute("SELECT * FROM channels WHERE live=1 ORDER BY name").fetchall()
        else:
            rows = conn.execute("SELECT * FROM channels ORDER BY name").fetchall()
        return [_normalize_channel_row(row) for row in rows]
    finally:
        conn.close()

def stop_recording(name):
    """Disable recording for a source name or every source sharing an archive slug."""
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE channels SET record=0 WHERE name=? OR archive_name=?",
            (name, name),
        )
        conn.commit()
        count = cur.rowcount if cur is not None else 0
        return max(0, int(count or 0))
    finally:
        conn.close()


def channels_for_archive(archive_name):
    """Return channel config dicts that belong to the given archive slug."""
    archive = str(archive_name or "").strip()
    if not archive:
        return []
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT * FROM channels
            WHERE name=? OR archive_name=?
            ORDER BY name
            """,
            (archive, archive),
        ).fetchall()
        return [_normalize_channel_row(row) for row in rows]
    finally:
        conn.close()


def set_channel_record(name, enabled: bool):
    """Enable or disable recording for a single source channel."""
    channel = str(name or "").strip()
    if not channel:
        return False
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE channels SET record=? WHERE name=?",
            (1 if enabled else 0, channel),
        )
        conn.commit()
        updated = cur.rowcount if cur is not None else 0
        return updated > 0
    finally:
        conn.close()


def has_active_task(task_type, streamer=None, filename=None, *, object_id=None, provider_id=None):
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT payload FROM tasks
            WHERE type=? AND status IN ('pending', 'running')
            ORDER BY id DESC
            LIMIT 1000
            """,
            (str(task_type or ""),),
        ).fetchall()
    finally:
        conn.close()
    want_streamer = str(streamer or "").strip()
    want_filename = str(filename or "").strip()
    want_object = None
    if object_id is not None:
        try:
            want_object = int(object_id)
        except (TypeError, ValueError):
            want_object = None
    want_provider = None
    if provider_id is not None:
        try:
            want_provider = int(provider_id)
        except (TypeError, ValueError):
            want_provider = None
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if want_provider is not None:
            try:
                if int(payload.get("provider_id")) != want_provider:
                    continue
            except (TypeError, ValueError):
                continue
        if want_object is not None:
            try:
                if int(payload.get("object_id")) == want_object:
                    return True
            except (TypeError, ValueError):
                pass
            continue
        if want_streamer and str(payload.get("streamer") or "").strip() != want_streamer:
            continue
        if want_filename and str(payload.get("filename") or "").strip() != want_filename:
            continue
        return True
    return False


def upsert_deleted_media(streamer, filename, file_id=None, deleted_by=None):
    """Remember an admin-deleted media item."""
    slug = str(streamer or "").strip()
    name = str(filename or "").strip()
    if not slug or not name or "/" in name or "\\" in name or name in (".", ".."):
        return False
    fid = str(file_id or "").strip() or None
    by = str(deleted_by or "").strip() or None
    now = time.time()
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO deleted_media (streamer, filename, file_id, deleted_at, deleted_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(streamer, filename) DO UPDATE SET
                file_id=COALESCE(excluded.file_id, deleted_media.file_id),
                deleted_at=excluded.deleted_at,
                deleted_by=COALESCE(excluded.deleted_by, deleted_media.deleted_by)
            """,
            (slug, name, fid, now, by),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def is_deleted_media(streamer=None, filename=None, file_id=None, streamers=None):
    """True if this media was admin-deleted (by file_id and/or streamer+filename)."""
    fid = str(file_id or "").strip()
    name = str(filename or "").strip()
    slugs = []
    if streamers:
        slugs = [str(s or "").strip() for s in streamers if str(s or "").strip()]
    slug = str(streamer or "").strip()
    if slug and slug not in slugs:
        slugs.append(slug)
    conn = get_db()
    try:
        if fid:
            row = conn.execute(
                "SELECT 1 FROM deleted_media WHERE file_id=? LIMIT 1",
                (fid,),
            ).fetchone()
            if row:
                return True
        if name and slugs:
            placeholders = ",".join("?" for _ in slugs)
            row = conn.execute(
                f"SELECT 1 FROM deleted_media WHERE filename=? AND streamer IN ({placeholders}) LIMIT 1",
                (name, *slugs),
            ).fetchone()
            return bool(row)
        return False
    finally:
        conn.close()


def get_deleted_media_set(streamers=None) -> tuple[set[str], set[str]]:
    """Return (filenames_set, file_ids_set) of deleted media in a single fast query."""
    slugs = []
    if streamers:
        slugs = [str(s or "").strip() for s in streamers if str(s or "").strip()]
    conn = get_db()
    try:
        if slugs:
            placeholders = ",".join("?" for _ in slugs)
            rows = conn.execute(
                f"SELECT filename, file_id FROM deleted_media WHERE streamer IN ({placeholders})",
                slugs,
            ).fetchall()
        else:
            rows = conn.execute("SELECT filename, file_id FROM deleted_media").fetchall()
        filenames = {str(r["filename"]).strip() for r in rows if r["filename"]}
        fids = {str(r["file_id"]).strip() for r in rows if r["file_id"]}
        return filenames, fids
    finally:
        conn.close()


def list_role_vod_access(role: str) -> list[str]:
    role_key = _normalize_vod_access_role(role)
    if not role_key:
        return []
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT streamer FROM role_vod_access WHERE role=? ORDER BY streamer COLLATE NOCASE",
            (role_key,),
        ).fetchall()
        return [str(r["streamer"]) for r in rows if r["streamer"]]
    finally:
        conn.close()


def set_role_vod_access(role: str, streamers) -> list[str]:
    role_key = _normalize_vod_access_role(role)
    if not role_key:
        return []
    cleaned = []
    seen = set()
    for raw in streamers or []:
        slug = _normalize_vod_access_slug(raw)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        cleaned.append(slug)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM role_vod_access WHERE role=?", (role_key,))
        for slug in cleaned:
            conn.execute(
                "INSERT INTO role_vod_access (role, streamer) VALUES (?, ?)",
                (role_key, slug),
            )
        # Keep legacy defaults table in sync for Discord-Member.
        if role_key == _ROLE_VOD_ACCESS_DEFAULT_ROLE:
            conn.execute("DELETE FROM vod_access_defaults")
            for slug in cleaned:
                conn.execute(
                    "INSERT INTO vod_access_defaults (streamer) VALUES (?)",
                    (slug,),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return cleaned


def add_role_vod_access(role: str, streamer: str) -> str | None:
    role_key = _normalize_vod_access_role(role)
    slug = _normalize_vod_access_slug(streamer)
    if not role_key or not slug:
        return None
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO role_vod_access (role, streamer) VALUES (?, ?)",
            (role_key, slug),
        )
        if role_key == _ROLE_VOD_ACCESS_DEFAULT_ROLE:
            conn.execute(
                "INSERT OR IGNORE INTO vod_access_defaults (streamer) VALUES (?)",
                (slug,),
            )
        conn.commit()
    finally:
        conn.close()
    return slug


def remove_role_vod_access(role: str, streamer: str) -> bool:
    role_key = _normalize_vod_access_role(role)
    slug = _normalize_vod_access_slug(streamer)
    if not role_key or not slug:
        return False
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM role_vod_access WHERE role=? AND streamer=?",
            (role_key, slug),
        )
        if role_key == _ROLE_VOD_ACCESS_DEFAULT_ROLE:
            conn.execute("DELETE FROM vod_access_defaults WHERE streamer=?", (slug,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_vod_access_defaults() -> list[str]:
    """Discord-Member role allowlist (legacy name kept for callers)."""
    rows = list_role_vod_access(_ROLE_VOD_ACCESS_DEFAULT_ROLE)
    if rows:
        return rows
    # Fallback seed path if migration has not run yet.
    conn = get_db()
    try:
        legacy = conn.execute(
            "SELECT streamer FROM vod_access_defaults ORDER BY streamer COLLATE NOCASE"
        ).fetchall()
        return [str(r["streamer"]) for r in legacy if r["streamer"]]
    finally:
        conn.close()


def set_vod_access_defaults(streamers) -> list[str]:
    return set_role_vod_access(_ROLE_VOD_ACCESS_DEFAULT_ROLE, streamers)


def add_vod_access_default(streamer: str) -> str | None:
    return add_role_vod_access(_ROLE_VOD_ACCESS_DEFAULT_ROLE, streamer)


def remove_vod_access_default(streamer: str) -> bool:
    return remove_role_vod_access(_ROLE_VOD_ACCESS_DEFAULT_ROLE, streamer)


def _row_to_user(row) -> dict | None:
    if row is None:
        return None
    keys = row.keys()
    return {
        "username": row["username"],
        "password_hash": row["password_hash"],
        "role": row["role"] if "role" in keys else "viewer",
        "active": bool(row["active"]) if "active" in keys else True,
        "is_active": bool(row["active"]) if "active" in keys else True,
        "group_id": int(row["group_id"]) if "group_id" in keys and row["group_id"] is not None else None,
        "email": (row["email"] if "email" in keys else None) or "",
        "updated_at": float(row["updated_at"] or 0) if "updated_at" in keys else 0.0,
        "discord_id": (row["discord_id"] if "discord_id" in keys else None) or "",
        "discord_username": (row["discord_username"] if "discord_username" in keys else None) or "",
        "created_via": (row["created_via"] if "created_via" in keys else None) or "",
        "created_at": float(row["created_at"] or 0) if "created_at" in keys else 0.0,
        "register_note": (row["register_note"] if "register_note" in keys else None) or "",
        "rules_accepted_at": float(row["rules_accepted_at"] or 0) if "rules_accepted_at" in keys else 0.0,
        "rules_version": (row["rules_version"] if "rules_version" in keys else None) or "",
        "terms_accepted_at": float(row["terms_accepted_at"] or 0) if "terms_accepted_at" in keys else 0.0,
        "terms_version": (row["terms_version"] if "terms_version" in keys else None) or "",
        "last_seen_at": float(row["last_seen_at"] or 0) if "last_seen_at" in keys else 0.0,
        "last_login_at": float(row["last_login_at"] or 0) if "last_login_at" in keys else 0.0,
        "session_version": str(row["session_version"] or "") if "session_version" in keys else "",
    }


def list_users() -> list[dict]:
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE").fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            logger.debug("Table 'users' not initialized yet: %s", exc)
            rows = []
        else:
            logger.error("Failed to list users from database: %s", exc)
            raise
    finally:
        conn.close()
    return [_row_to_user(r) for r in rows]


def get_user(username: str) -> dict | None:
    username = str(username or "").strip()
    if not username:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_user(row)


def get_user_by_discord_id(discord_id: str) -> dict | None:
    discord_id = str(discord_id or "").strip()
    if not discord_id:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE discord_id = ?",
            (discord_id,),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_user(row)


def list_pending_users() -> list[dict]:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM users WHERE active = 0 ORDER BY COALESCE(created_at, 0) DESC, username COLLATE NOCASE"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_user(r) for r in rows]


def upsert_user(
    username: str,
    password_hash: str,
    *,
    role: str = "viewer",
    active: bool = True,
    group_id: int | None = None,
    email: str | None = None,
    discord_id: str | None = None,
    discord_username: str | None = None,
    created_via: str | None = None,
    register_note: str | None = None,
    rules_accepted_at: float | None = None,
    rules_version: str | None = None,
    terms_accepted_at: float | None = None,
    terms_version: str | None = None,
    preserve_discord: bool = True,
) -> None:
    username = str(username or "").strip()
    if not username:
        raise ValueError("username required")
    existing = get_user(username)
    now = time.time()
    if preserve_discord and existing:
        if group_id is None:
            group_id = existing.get("group_id")
        if email is None:
            email = existing.get("email") or ""
        if discord_id is None:
            discord_id = existing.get("discord_id") or None
        if discord_username is None:
            discord_username = existing.get("discord_username") or None
        if created_via is None:
            created_via = existing.get("created_via") or None
        if register_note is None:
            register_note = existing.get("register_note") or None
        if rules_accepted_at is None:
            rules_accepted_at = existing.get("rules_accepted_at") or None
        if rules_version is None:
            rules_version = existing.get("rules_version") or None
        if terms_accepted_at is None:
            terms_accepted_at = existing.get("terms_accepted_at") or None
        if terms_version is None:
            terms_version = existing.get("terms_version") or None
        created_at = existing.get("created_at") or now
    else:
        created_at = now if not existing else (existing.get("created_at") or now)

    if group_id is None:
        group_id = 1 if role == "admin" else 3
    email = str(email or "").strip()

    discord_id_v = (str(discord_id).strip() if discord_id else "") or None
    discord_username_v = (str(discord_username).strip() if discord_username else "") or None
    created_via_v = (str(created_via).strip() if created_via else "") or None
    register_note_v = (str(register_note).strip() if register_note else "") or None
    if register_note_v:
        register_note_v = register_note_v[:300]
    try:
        rules_accepted_at_v = float(rules_accepted_at) if rules_accepted_at else None
    except (TypeError, ValueError):
        rules_accepted_at_v = None
    rules_version_v = (str(rules_version).strip() if rules_version else "") or None
    try:
        terms_accepted_at_v = float(terms_accepted_at) if terms_accepted_at else None
    except (TypeError, ValueError):
        terms_accepted_at_v = None
    terms_version_v = (str(terms_version).strip() if terms_version else "") or None

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO users (
                username, password_hash, role, active, group_id, email,
                discord_id, discord_username, created_via, created_at, updated_at, register_note,
                rules_accepted_at, rules_version, terms_accepted_at, terms_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                password_hash=excluded.password_hash,
                role=excluded.role,
                active=excluded.active,
                group_id=COALESCE(excluded.group_id, users.group_id),
                email=COALESCE(excluded.email, users.email),
                discord_id=COALESCE(excluded.discord_id, users.discord_id),
                discord_username=COALESCE(excluded.discord_username, users.discord_username),
                created_via=COALESCE(excluded.created_via, users.created_via),
                register_note=COALESCE(excluded.register_note, users.register_note),
                rules_accepted_at=COALESCE(excluded.rules_accepted_at, users.rules_accepted_at),
                rules_version=COALESCE(excluded.rules_version, users.rules_version),
                terms_accepted_at=COALESCE(excluded.terms_accepted_at, users.terms_accepted_at),
                terms_version=COALESCE(excluded.terms_version, users.terms_version),
                created_at=COALESCE(users.created_at, excluded.created_at),
                updated_at=excluded.updated_at
            """,
            (
                username,
                password_hash,
                role,
                1 if active else 0,
                group_id,
                email,
                discord_id_v,
                discord_username_v,
                created_via_v,
                created_at,
                now,
                register_note_v,
                rules_accepted_at_v,
                rules_version_v,
                terms_accepted_at_v,
                terms_version_v,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_user_rules_acceptance(
    username: str,
    *,
    version: str,
    accepted_at: float | None = None,
) -> None:
    username = str(username or "").strip()
    version = str(version or "").strip()
    if not username or not version:
        raise ValueError("username and version required")
    ts = float(accepted_at if accepted_at is not None else time.time())
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE users
            SET rules_accepted_at = ?, rules_version = ?
            WHERE username = ? COLLATE NOCASE
            """,
            (ts, version, username),
        )
        conn.commit()
    finally:
        conn.close()


def set_user_terms_acceptance(
    username: str,
    *,
    terms_version: str,
    terms_accepted_at: float | None = None,
    rules_version: str | None = None,
    rules_accepted_at: float | None = None,
) -> None:
    username = str(username or "").strip()
    terms_version = str(terms_version or "").strip()
    if not username or not terms_version:
        raise ValueError("username and terms_version required")
    ts = float(terms_accepted_at if terms_accepted_at is not None else time.time())
    rules_ver = (str(rules_version).strip() if rules_version else "") or None
    try:
        rules_ts = float(rules_accepted_at) if rules_accepted_at is not None else (ts if rules_ver else None)
    except (TypeError, ValueError):
        rules_ts = ts if rules_ver else None
    conn = get_db()
    try:
        if rules_ver:
            conn.execute(
                """
                UPDATE users
                SET terms_accepted_at = ?, terms_version = ?,
                    rules_accepted_at = ?, rules_version = ?
                WHERE username = ? COLLATE NOCASE
                """,
                (ts, terms_version, rules_ts, rules_ver, username),
            )
        else:
            conn.execute(
                """
                UPDATE users
                SET terms_accepted_at = ?, terms_version = ?
                WHERE username = ? COLLATE NOCASE
                """,
                (ts, terms_version, username),
            )
        conn.commit()
    finally:
        conn.close()


def add_terms_acceptance(
    *,
    username: str,
    discord_id: str = "",
    terms_version: str,
    rules_version: str = "",
    accepted_at: float | None = None,
    source: str = "web",
) -> None:
    username = str(username or "").strip()
    terms_version = str(terms_version or "").strip()
    if not username or not terms_version:
        raise ValueError("username and terms_version required")
    ts = float(accepted_at if accepted_at is not None else time.time())
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO terms_acceptances (
                username, discord_id, terms_version, rules_version, accepted_at, source
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                (str(discord_id).strip() or None),
                terms_version,
                (str(rules_version).strip() or None),
                ts,
                (str(source).strip() or "web")[:32],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def list_terms_acceptances(limit: int = 200) -> list[dict]:
    try:
        cap = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        cap = 200
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT id, username, discord_id, terms_version, rules_version, accepted_at, source
            FROM terms_acceptances
            ORDER BY accepted_at DESC, id DESC
            LIMIT ?
            """,
            (cap,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        out.append(
            {
                "id": row["id"],
                "username": row["username"] or "",
                "discord_id": row["discord_id"] or "",
                "terms_version": row["terms_version"] or "",
                "rules_version": row["rules_version"] or "",
                "accepted_at": float(row["accepted_at"] or 0),
                "source": row["source"] or "",
            }
        )
    return out


def set_user_active(username: str, active: bool) -> None:
    conn = get_db()
    try:
        conn.execute(
            "UPDATE users SET active = ? WHERE username = ? COLLATE NOCASE",
            (1 if active else 0, username),
        )
        conn.commit()
    finally:
        conn.close()


def set_user_role(username: str, role: str) -> None:
    conn = get_db()
    try:
        conn.execute(
            "UPDATE users SET role = ? WHERE username = ? COLLATE NOCASE",
            (role, username),
        )
        conn.commit()
    finally:
        conn.close()


@db_retry(max_retries=5, delay=0.1, backoff=2.0)
def set_user_password(username: str, password_hash: str) -> str:
    """Store a new hash and revoke every existing session of the user.

    Returns the new session token ("" when the user does not exist).
    """
    token = secrets.token_hex(16)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE users SET password_hash = ?, session_version = ?, updated_at = ? "
            "WHERE username = ? COLLATE NOCASE",
            (password_hash, token, time.time(), username),
        )
        conn.commit()
    finally:
        conn.close()
    return token if cur.rowcount else ""


@db_retry(max_retries=5, delay=0.1, backoff=2.0)
def bump_session_version(username: str) -> str:
    """Invalidate all login cookies of a user. Returns the new token ("" if unknown).

    The token is random rather than a counter, so a value seen once (in an old
    cookie) can never become valid again.
    """
    name = str(username or "").strip()
    if not name:
        return ""
    token = secrets.token_hex(16)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE users SET session_version = ? WHERE username = ? COLLATE NOCASE",
            (token, name),
        )
        conn.commit()
    finally:
        conn.close()
    return token if cur.rowcount else ""


def set_user_email(username: str, email: str) -> None:
    name = str(username or "").strip()
    conn = get_db()
    try:
        conn.execute(
            "UPDATE users SET email = ?, updated_at = ? WHERE username = ? COLLATE NOCASE",
            (str(email or "").strip(), time.time(), name),
        )
        conn.commit()
    finally:
        conn.close()


def delete_user(username: str) -> None:
    name = str(username or "").strip()
    conn = get_db()
    try:
        if name:
            conn.execute("DELETE FROM user_vod_access WHERE username=? COLLATE NOCASE", (name,))
            conn.execute("DELETE FROM user_traffic_daily WHERE username=? COLLATE NOCASE", (name,))
            conn.execute("DELETE FROM vod_watch_slots WHERE holder=? COLLATE NOCASE", (name,))
        conn.execute("DELETE FROM users WHERE username = ? COLLATE NOCASE", (username,))
        conn.commit()
    finally:
        conn.close()


def traffic_day_key(ts: float | None = None) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz_name = os.environ.get("TZ") or "Europe/Berlin"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Berlin")
    stamp = time.time() if ts is None else float(ts)
    return datetime.fromtimestamp(stamp, tz).strftime("%Y-%m-%d")


def _backfill_user_traffic_from_login_logs(cursor) -> None:
    """One-shot: successful historic logins → daily login counters + last_login_at."""
    try:
        cursor.execute(
            "INSERT INTO user_traffic_meta(key, value) VALUES ('login_backfill', '1')"
        )
    except sqlite3.IntegrityError:
        return
    except sqlite3.OperationalError:
        return
    try:
        rows = cursor.execute(
            "SELECT username, ts FROM login_logs WHERE success=1 AND username IS NOT NULL AND username != ''"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    last_login = {}
    for row in rows:
        username = str(row["username"] or "").strip()
        if not username:
            continue
        try:
            ts = float(row["ts"] or 0)
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        day = traffic_day_key(ts)
        cursor.execute(
            """
            INSERT INTO user_traffic_daily (username, day, logins, pages, media, chats)
            VALUES (?, ?, 1, 0, 0, 0)
            ON CONFLICT(username, day) DO UPDATE SET logins = logins + 1
            """,
            (username, day),
        )
        prev = last_login.get(username.lower())
        if prev is None or ts > prev[1]:
            last_login[username.lower()] = (username, ts)
    for username, ts in last_login.values():
        cursor.execute(
            """
            UPDATE users SET last_login_at = CASE
                WHEN last_login_at IS NULL OR last_login_at < ? THEN ?
                ELSE last_login_at
            END,
            last_seen_at = CASE
                WHEN last_seen_at IS NULL OR last_seen_at < ? THEN ?
                ELSE last_seen_at
            END
            WHERE username = ? COLLATE NOCASE
            """,
            (ts, ts, ts, ts, username),
        )


def increment_user_traffic(username: str, kind: str, ts: float | None = None) -> None:
    username = str(username or "").strip()
    kind = str(kind or "").strip().lower()
    if not username or kind not in {"login", "page", "media", "chat"}:
        return
    stamp = time.time() if ts is None else float(ts)
    day = traffic_day_key(stamp)
    column = {"login": "logins", "page": "pages", "media": "media", "chat": "chats"}[kind]
    conn = get_db()
    try:
        conn.execute(
            f"""
            INSERT INTO user_traffic_daily (username, day, logins, pages, media, chats)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(username, day) DO UPDATE SET {column} = {column} + 1
            """,
            (
                username,
                day,
                1 if kind == "login" else 0,
                1 if kind == "page" else 0,
                1 if kind == "media" else 0,
                1 if kind == "chat" else 0,
            ),
        )
        conn.execute(
            """
            UPDATE users SET last_seen_at = CASE
                WHEN last_seen_at IS NULL OR last_seen_at < ? THEN ?
                ELSE last_seen_at
            END
            WHERE username = ? COLLATE NOCASE
            """,
            (stamp, stamp, username),
        )
        if kind == "login":
            conn.execute(
                """
                UPDATE users SET last_login_at = CASE
                    WHEN last_login_at IS NULL OR last_login_at < ? THEN ?
                    ELSE last_login_at
                END
                WHERE username = ? COLLATE NOCASE
                """,
                (stamp, stamp, username),
            )
        conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if "no such table" in str(exc).lower():
            logger.debug("Table 'user_traffic_daily' not initialized yet: %s", exc)
        else:
            logger.warning("Failed to increment user traffic: %s", exc)
    finally:
        conn.close()


def list_user_traffic_daily(since_day: str) -> list[dict]:
    since_day = str(since_day or "").strip()
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT username, day, logins, pages, media, chats
            FROM user_traffic_daily
            WHERE day >= ?
            ORDER BY username COLLATE NOCASE, day
            """,
            (since_day,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        out.append(
            {
                "username": row["username"],
                "day": row["day"],
                "logins": int(row["logins"] or 0),
                "pages": int(row["pages"] or 0),
                "media": int(row["media"] or 0),
                "chats": int(row["chats"] or 0),
            }
        )
    return out


# ---------------------------------------------------------------------------
# RBAC: User Groups and Dynamic Permission Management
# ---------------------------------------------------------------------------

def _normalize_group_permissions(perms: dict | None) -> dict:
    p = dict(perms or {})
    p.setdefault("can_view_streams", True)
    p.setdefault("can_download_archive", False)
    p.setdefault("can_upload_videos", False)
    p.setdefault("can_delete_videos", False)
    p.setdefault("can_manage_users", False)
    p.setdefault("access_all_channels", True)
    p.setdefault("allowed_channels", [])
    p.setdefault("disabled_channels", [])
    if not isinstance(p["allowed_channels"], list):
        p["allowed_channels"] = []
    if not isinstance(p["disabled_channels"], list):
        p["disabled_channels"] = []
    return p


def list_user_groups() -> list[dict]:
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT g.*, COUNT(u.username) AS member_count
            FROM user_groups g
            LEFT JOIN users u ON u.group_id = g.id
            GROUP BY g.id
            ORDER BY g.is_system DESC, g.name ASC
            """
        ).fetchall()
        result = []
        for r in rows:
            perms = {}
            try:
                perms = json.loads(r["permissions"]) if r["permissions"] else {}
            except (TypeError, ValueError) as exc:
                logger.warning("Group %s has unreadable permissions, using none: %s", r["id"], exc)
            result.append({
                "id": r["id"],
                "name": r["name"],
                "description": r["description"] or "",
                "permissions": _normalize_group_permissions(perms),
                "is_system": bool(r["is_system"]),
                "member_count": int(r["member_count"] or 0),
                "created_at": float(r["created_at"] or 0),
                "updated_at": float(r["updated_at"] or 0),
            })
        return result
    finally:
        conn.close()


def get_user_group(group_id: int | str) -> dict | None:
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        return None
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT g.*, COUNT(u.username) AS member_count
            FROM user_groups g
            LEFT JOIN users u ON u.group_id = g.id
            WHERE g.id = ?
            GROUP BY g.id
            """,
            (gid,),
        ).fetchone()
        if not row:
            return None
        perms = {}
        try:
            perms = json.loads(row["permissions"]) if row["permissions"] else {}
        except (TypeError, ValueError) as exc:
            logger.warning("Group %s has unreadable permissions, using none: %s", row["id"], exc)
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"] or "",
            "permissions": _normalize_group_permissions(perms),
            "is_system": bool(row["is_system"]),
            "member_count": int(row["member_count"] or 0),
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
        }
    finally:
        conn.close()


def get_user_group_by_name(name: str) -> dict | None:
    name = str(name or "").strip()
    if not name:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT g.*, COUNT(u.username) AS member_count
            FROM user_groups g
            LEFT JOIN users u ON u.group_id = g.id
            WHERE g.name = ? COLLATE NOCASE
            GROUP BY g.id
            """,
            (name,),
        ).fetchone()
        if not row:
            return None
        perms = {}
        try:
            perms = json.loads(row["permissions"]) if row["permissions"] else {}
        except (TypeError, ValueError) as exc:
            logger.warning("Group %s has unreadable permissions, using none: %s", row["id"], exc)
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"] or "",
            "permissions": _normalize_group_permissions(perms),
            "is_system": bool(row["is_system"]),
            "member_count": int(row["member_count"] or 0),
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
        }
    finally:
        conn.close()


def create_user_group(
    name: str,
    description: str = "",
    permissions: dict | None = None,
    is_system: bool = False,
) -> int:
    name = str(name or "").strip()
    if not (2 <= len(name) <= 50):
        raise GroupError(N_("Group name must be between 2 and 50 characters long."))
    existing = get_user_group_by_name(name)
    if existing:
        raise GroupError(N_("A group named '%(name)s' already exists."), name=name)

    default_perms = {
        "can_view_streams": True,
        "can_download_archive": False,
        "can_upload_videos": False,
        "can_delete_videos": False,
        "can_manage_users": False,
        "access_all_channels": True,
        "allowed_channels": [],
        "disabled_channels": [],
    }
    if permissions:
        for k, v in permissions.items():
            if k in ("allowed_channels", "disabled_channels"):
                default_perms[k] = [str(c).strip() for c in (v or []) if str(c).strip()]
            else:
                default_perms[k] = bool(v)

    now = time.time()
    conn = get_db()
    try:
        cur = conn.execute(
            """
            INSERT INTO user_groups (name, description, permissions, is_system, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, str(description or "").strip(), json.dumps(default_perms), 1 if is_system else 0, now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_user_group(
    group_id: int,
    name: str,
    description: str = "",
    permissions: dict | None = None,
) -> bool:
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        raise GroupError(N_("Invalid group ID.")) from None
    group = get_user_group(gid)
    if not group:
        raise GroupError(N_("Group not found."))

    name_clean = str(name or "").strip()
    if not (2 <= len(name_clean) <= 50):
        raise GroupError(N_("Group name must be between 2 and 50 characters long."))

    # System group name protection
    if group["is_system"] and group["name"].lower() != name_clean.lower():
        raise GroupError(N_("The name of system groups cannot be changed."))

    # Check duplicate name if name changed
    if group["name"].lower() != name_clean.lower():
        duplicate = get_user_group_by_name(name_clean)
        if duplicate and duplicate["id"] != gid:
            raise GroupError(N_("A group named '%(name)s' already exists."), name=name_clean)

    merged_perms = _normalize_group_permissions(group["permissions"])
    if permissions is not None:
        for k, v in permissions.items():
            if k in ("allowed_channels", "disabled_channels"):
                merged_perms[k] = [str(c).strip() for c in (v or []) if str(c).strip()]
            else:
                merged_perms[k] = bool(v)

    # Lockout protection: Administrator group cannot lose can_manage_users
    if group["name"] == "Administrator" and group["is_system"]:
        if not merged_perms.get("can_manage_users"):
            raise GroupError(N_("The 'can_manage_users' permission cannot be removed from the Administrator group."))

    now = time.time()
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE user_groups
            SET name = ?, description = ?, permissions = ?, updated_at = ?
            WHERE id = ?
            """,
            (name_clean, str(description or "").strip(), json.dumps(merged_perms), now, gid),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def delete_user_group(group_id: int) -> bool:
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        raise GroupError(N_("Invalid group ID.")) from None
    group = get_user_group(gid)
    if not group:
        raise GroupError(N_("Group not found."))
    if group["is_system"]:
        raise GroupError(N_("System groups (%(name)s) cannot be deleted."), name=group["name"])

    viewer_group = get_user_group_by_name("Viewer")
    if not viewer_group:
        raise GroupError(N_("System group 'Viewer' was not found. Deletion cancelled."))

    now = time.time()
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE users SET group_id = ?, updated_at = ? WHERE group_id = ?",
            (viewer_group["id"], now, gid),
        )
        conn.execute("DELETE FROM user_groups WHERE id = ?", (gid,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_user_group(username: str, group_id: int) -> bool:
    username = str(username or "").strip()
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        raise GroupError(N_("Invalid group ID.")) from None
    group = get_user_group(gid)
    if not group:
        raise GroupError(N_("Group not found."))

    now = time.time()
    conn = get_db()
    try:
        conn.execute(
            "UPDATE users SET group_id = ?, updated_at = ? WHERE username = ? COLLATE NOCASE",
            (gid, now, username),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_user_permissions(username: str) -> dict:
    user = get_user(username)
    if not user or not user.get("active"):
        return {
            "can_view_streams": False,
            "can_download_archive": False,
            "can_upload_videos": False,
            "can_delete_videos": False,
            "can_manage_users": False,
            "access_all_channels": False,
            "allowed_channels": [],
            "disabled_channels": [],
        }
    gid = user.get("group_id")
    if gid:
        group = get_user_group(gid)
        if group and group.get("permissions"):
            return _normalize_group_permissions(group["permissions"])
    role = user.get("role", "viewer")
    if role == "admin":
        return {
            "can_view_streams": True,
            "can_download_archive": True,
            "can_upload_videos": True,
            "can_delete_videos": True,
            "can_manage_users": True,
            "access_all_channels": True,
            "allowed_channels": [],
            "disabled_channels": [],
        }
    return {
        "can_view_streams": True,
        "can_download_archive": False,
        "can_upload_videos": False,
        "can_delete_videos": False,
        "can_manage_users": False,
        "access_all_channels": True,
        "allowed_channels": [],
        "disabled_channels": [],
    }


# ---------------------------------------------------------------------------
# External storage providers and archive objects
# ---------------------------------------------------------------------------

def _storage_provider_row(row):
    if not row:
        return None
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    data["last_check_ok"] = (
        None if data.get("last_check_ok") is None else bool(data.get("last_check_ok"))
    )
    return data


def list_storage_providers():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM storage_providers ORDER BY name COLLATE NOCASE ASC"
        ).fetchall()
        return [_storage_provider_row(row) for row in rows]
    finally:
        conn.close()


def get_storage_provider(provider_id):
    try:
        pid = int(provider_id)
    except (TypeError, ValueError):
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM storage_providers WHERE id = ?",
            (pid,),
        ).fetchone()
        return _storage_provider_row(row)
    finally:
        conn.close()


def create_storage_provider(name, provider_type, config, enabled=True):
    now = time.time()
    conn = get_db()
    try:
        cur = conn.execute(
            """
            INSERT INTO storage_providers
                (name, provider_type, config, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(name or "").strip(),
                str(provider_type or "").strip(),
                str(config or ""),
                1 if enabled else 0,
                now,
                now,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_storage_provider(
    provider_id,
    *,
    name=None,
    provider_type=None,
    config=None,
    enabled=None,
):
    try:
        pid = int(provider_id)
    except (TypeError, ValueError):
        return False
    fields = []
    values = []
    if name is not None:
        fields.append("name = ?")
        values.append(str(name or "").strip())
    if provider_type is not None:
        fields.append("provider_type = ?")
        values.append(str(provider_type or "").strip())
    if config is not None:
        fields.append("config = ?")
        values.append(str(config or ""))
    if enabled is not None:
        fields.append("enabled = ?")
        values.append(1 if enabled else 0)
    if not fields:
        return False
    fields.append("updated_at = ?")
    values.append(time.time())
    values.append(pid)
    conn = get_db()
    try:
        cur = conn.execute(
            f"UPDATE storage_providers SET {', '.join(fields)} WHERE id = ?",
            tuple(values),
        )
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def delete_storage_provider(provider_id):
    try:
        pid = int(provider_id)
    except (TypeError, ValueError):
        return False
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM storage_objects WHERE provider_id = ?", (pid,))
        cur = conn.execute("DELETE FROM storage_providers WHERE id = ?", (pid,))
        conn.commit()
        return bool(cur.rowcount)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_storage_provider_check(provider_id, ok, message=""):
    try:
        pid = int(provider_id)
    except (TypeError, ValueError):
        return False
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE storage_providers
            SET last_check_at = ?, last_check_ok = ?, last_check_message = ?, updated_at = ?
            WHERE id = ?
            """,
            (time.time(), 1 if ok else 0, str(message or "")[:500], time.time(), pid),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def set_storage_provider_error(provider_id, error=""):
    try:
        pid = int(provider_id)
    except (TypeError, ValueError):
        return False
    conn = get_db()
    try:
        conn.execute(
            "UPDATE storage_providers SET last_error = ?, updated_at = ? WHERE id = ?",
            (str(error or "")[:500], time.time(), pid),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def _storage_object_row(row):
    return dict(row) if row else None


def get_storage_object(object_id):
    try:
        oid = int(object_id)
    except (TypeError, ValueError):
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM storage_objects WHERE id = ?",
            (oid,),
        ).fetchone()
        return _storage_object_row(row)
    finally:
        conn.close()


def find_storage_object(provider_id, local_path):
    try:
        pid = int(provider_id)
    except (TypeError, ValueError):
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM storage_objects WHERE provider_id = ? AND local_path = ?",
            (pid, str(local_path or "")),
        ).fetchone()
        return _storage_object_row(row)
    finally:
        conn.close()


def upsert_storage_object(
    provider_id,
    local_path,
    remote_path,
    *,
    streamer="",
    filename="",
    kind="vod",
    local_size=0,
    local_mtime=0,
    status="pending",
):
    try:
        pid = int(provider_id)
    except (TypeError, ValueError):
        raise ValueError("Ungültige Provider-ID.") from None
    now = time.time()
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO storage_objects
                (provider_id, local_path, remote_path, streamer, filename, kind,
                 status, local_size, local_mtime, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_id, local_path) DO UPDATE SET
                remote_path = excluded.remote_path,
                streamer = excluded.streamer,
                filename = excluded.filename,
                kind = excluded.kind,
                updated_at = excluded.updated_at
            """,
            (
                pid,
                str(local_path or ""),
                str(remote_path or ""),
                str(streamer or ""),
                str(filename or ""),
                str(kind or "vod"),
                str(status or "pending"),
                max(0, int(local_size or 0)),
                float(local_mtime or 0),
                now,
                now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM storage_objects WHERE provider_id = ? AND local_path = ?",
            (pid, str(local_path or "")),
        ).fetchone()
        return _storage_object_row(row)
    finally:
        conn.close()


_STORAGE_OBJECT_WRITABLE = {
    "remote_path",
    "streamer",
    "filename",
    "kind",
    "status",
    "local_size",
    "local_mtime",
    "remote_size",
    "remote_modified",
    "remote_checksum",
    "progress",
    "attempts",
    "last_error",
    "uploaded_at",
    "local_deleted_at",
    "remote_deleted_at",
}


def update_storage_object(object_id, **fields):
    try:
        oid = int(object_id)
    except (TypeError, ValueError):
        return False
    updates = {key: value for key, value in fields.items() if key in _STORAGE_OBJECT_WRITABLE}
    if not updates:
        return False
    if "last_error" in updates:
        updates["last_error"] = str(updates["last_error"] or "")[:500]
    columns = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values())
    values.extend([time.time(), oid])
    conn = get_db()
    try:
        cur = conn.execute(
            f"UPDATE storage_objects SET {columns}, updated_at = ? WHERE id = ?",
            tuple(values),
        )
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def list_storage_objects(
    provider_id=None,
    status=None,
    streamer=None,
    *,
    limit=200,
    offset=0,
    uploaded_before=None,
    local_deleted=None,
    remote_deleted=False,
):
    clauses = []
    values = []
    if provider_id is not None:
        try:
            clauses.append("provider_id = ?")
            values.append(int(provider_id))
        except (TypeError, ValueError):
            return []
    if status:
        statuses = [status] if isinstance(status, str) else list(status)
        statuses = [str(item) for item in statuses if item]
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            values.extend(statuses)
    if streamer:
        clauses.append("streamer = ? COLLATE NOCASE")
        values.append(str(streamer))
    if uploaded_before is not None:
        clauses.append("uploaded_at IS NOT NULL AND uploaded_at <= ?")
        values.append(float(uploaded_before))
    if local_deleted is True:
        clauses.append("local_deleted_at IS NOT NULL")
    elif local_deleted is False:
        clauses.append("local_deleted_at IS NULL")
    if remote_deleted:
        clauses.append("remote_deleted_at IS NOT NULL")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        limit_i = max(1, min(5000, int(limit)))
        offset_i = max(0, int(offset))
    except (TypeError, ValueError):
        limit_i, offset_i = 200, 0
    conn = get_db()
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM storage_objects
            {where}
            ORDER BY updated_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*values, limit_i, offset_i),
        ).fetchall()
        return [_storage_object_row(row) for row in rows]
    finally:
        conn.close()


def storage_object_summary(provider_id=None):
    conn = get_db()
    try:
        clauses = ["remote_deleted_at IS NULL", "status != 'remote_deleted'"]
        params = []
        if provider_id is not None:
            try:
                clauses.append("provider_id = ?")
                params.append(int(provider_id))
            except (TypeError, ValueError):
                return {"total": 0, "bytes": 0, "statuses": {}}
        where = "WHERE " + " AND ".join(clauses)
        rows = conn.execute(
            f"""
            SELECT status, COUNT(*) AS count, COALESCE(SUM(remote_size), 0) AS bytes
            FROM storage_objects {where}
            GROUP BY status
            """,
            tuple(params),
        ).fetchall()
        total = 0
        total_bytes = 0
        statuses = {}
        for row in rows:
            count = int(row["count"] or 0)
            total += count
            total_bytes += int(row["bytes"] or 0)
            statuses[str(row["status"] or "")] = {
                "count": count,
                "bytes": int(row["bytes"] or 0),
            }
        return {"total": total, "bytes": total_bytes, "statuses": statuses}
    finally:
        conn.close()


def delete_storage_object(object_id):
    try:
        oid = int(object_id)
    except (TypeError, ValueError):
        return False
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM storage_objects WHERE id = ?", (oid,))
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def list_storage_objects_for_media(streamer, filename):
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT * FROM storage_objects
            WHERE streamer = ? COLLATE NOCASE AND filename = ? COLLATE NOCASE
            ORDER BY updated_at DESC
            """,
            (str(streamer or ""), str(filename or "")),
        ).fetchall()
        return [_storage_object_row(row) for row in rows]
    finally:
        conn.close()
