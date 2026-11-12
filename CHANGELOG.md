# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Breaking changes

- **Sessions expire after 30 days** (`VOD_SESSION_MAX_AGE_SEC`, `0` restores
  the old unlimited behaviour). Logout and every password change sign the
  account out on all devices.
- **Overlay tokens in the query string are off by default.** The bundled
  TTS/alert overlay pages pass `?token=`; set `OVERLAY_ALLOW_QUERY_TOKEN=true`
  while you use them. The `/tts/api/ack` and `/alerts/api/ack` routes accept
  POST only.
- **Resource uploads use an allowlist** (documents, images, audio/video,
  archives; `VOD_FILES_ALLOWED_EXTENSIONS` overrides it). Existing files with
  other extensions are no longer served but can still be deleted.
- **Docker programs run as an unprivileged user** (`APP_UID`, default 1000).
  `./data` must be writable by that UID: `sudo chown -R 1000:1000 data`.

### Security

- Cancelling a transcription only signals a verified `run_transcribe.py`
  process group, never PID 1 or the web worker's own group.
- The Twitch OAuth token never falls back to the command line when its
  private config file cannot be written.
- Login throttling counts only form submissions, groups IPv6 clients by /64,
  adds a per-account limit and answers with HTTP 429 and `Retry-After`.
- Shared and downloaded resource files are always sent as
  `attachment`/`application/octet-stream` with `nosniff`; symlinked media
  and resource files are refused.
- Constant-time comparison for the environment admin password; unknown
  user names still run a hash check. `/healthz` shows disk data to admins
  only. Client values in VOD debug logs are stripped of control characters.

### Changed

- `install.sh` aborts on a failing `nginx -t`, copies without databases,
  `data/`, `demo-data/` or `.env`, runs git as the service user, keeps the
  initial password out of the terminal and does not restart services while
  recordings are running. `streamvault stop|restart|update` refuse the same
  unless `--force` is given; `restart` accepts a single service.
- `scripts/streamvault_vps.sh deploy` takes a `sqlite3 .backup` of the
  database before touching the code.
- The nginx example sets `Connection` via `map`, keeps security headers in
  every location with its own `add_header`, marks protected thumbnails
  `private` and compresses proxied CSS/JS (`gzip_proxied any`).
- `core.channels` holds the channel helpers shared by the web UI and the
  recorder; `core.database` no longer depends on Flask.

### Fixed

- Broad `except Exception: pass` handlers in the database layer, recorder,
  worker and web UI now catch specific errors and log them.

## [0.1.0] - 2026-11-09

### Breaking changes

- **Docker data directory.** `docker-compose.yml` now uses
  `VOD_BASE_DIR=/app/data`. Before upgrading, move archives,
  `streamrecorder.db` and the dot-directories (`.meta`, `.thumbs`, `.hls`,
  `.vodcache`) from the project root into `./data`.
- **Casting removed.** Chromecast/AirPlay buttons, the signed `?cast=` media
  URLs and `/api/live/cast-url` are gone. Media is only served to logged-in
  sessions and without `Access-Control-Allow-Origin: *`.
- **OBS pages need a login.** `/live/obs/<channel>` and `/chat/obs/<channel>`
  require a session; log in once inside the OBS browser source
  ("Interact").
- **Smart-TV mode removed**, including `/login/tv` and the gatekeeper
  auto-login.
- **Playlist proxy is opt-in only.** No proxy hosts are built in; set
  `TWITCH_PROXY_PLAYLIST` (and `STREAMLINK_PLUGIN_DIR` if needed) together
  with `STREAM_RECORDER_USE_PROXY=true`.
- **Backups.** `BACKUP_DIR` defaults to `<VOD_META_DIR>/backups`; "create
  backup" runs the script named in `VOD_BACKUP_SCRIPT`.

### Security

- Client IPs behind a proxy come from `X-Real-IP` or the right-most
  `X-Forwarded-For` hop; rotating `X-Forwarded-For` no longer bypasses login
  rate limits and bans. `VOD_TRUSTED_PROXIES` accepts CIDR networks.
- Code and dot-directories below `VOD_BASE_DIR` can no longer be deleted as
  streamer archives.

### Fixed

- `/agb` and `/datenschutz` returned HTTP 500 on fresh installs.
- The worker could remux a recording that was still being written when its
  status file was half-written or owned by another user.
- A corrupt JSON state file (favorites, profiles, shares, settings) was
  silently replaced by an empty one; it is now kept as `<file>.corrupt-<ts>`.
- Login heading overflowed its card; mobile admin pages clipped buttons,
  tabs and tables.
- Browsers kept stale CSS/JS after updates; static URLs now carry a version
  derived from the shipped files.
- Storage figures showed "N/A" on non-Linux hosts and swallowed errors
  silently; they now use `shutil.disk_usage` and log failures.
- The VOD chat panel was always labelled "Twitch Chat", untranslated, also
  for YouTube recordings.
- The sidebar language switcher was cut off to "A…"; it now shows a globe and
  the language code.
- Channel pages showed a stray separator when a recording had no size.

### Added

- GitHub Actions CI (Python 3.10–3.12, ruff, unit tests, shell syntax),
  Dependabot, contribution and security guidelines.
- Web UI in 12 languages, chosen from the browser language or the switcher.
- `scripts/demo_seed.py` creates a sample archive for trying StreamVault
  without recording anything.
