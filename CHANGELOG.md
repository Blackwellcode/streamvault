# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Breaking changes

- **New minimum versions, reinstall the requirements** after updating
  (`pip install -r requirements.txt` in the virtualenv): redis ≥ 8.1.0,
  streamlink ≥ 8.6.3, markupsafe ≥ 3.0.4, pycryptodome ≥ 4.4.0,
  numpy ≥ 2.2.6. StreamVault keeps redis-py 5 behaviour: RESP2 (Redis servers
  older than 6 keep working) and one retry on timeouts only, so an unreachable
  Redis still falls back to local mode after a single connect attempt.
- **Sessions expire after 30 days** (`VOD_SESSION_MAX_AGE_SEC`, `0` restores
  the old unlimited behaviour). Logout ends the session in this browser;
  the new "log out everywhere" action (`POST /logout/all`) and every password
  change sign the account out on all devices.
- **Overlay tokens in the query string are off by default.** The bundled
  TTS/alert overlays read the token from `#token=` (or an old `?token=` URL)
  and send it only as a header, so existing OBS sources keep working; move
  the token behind `#` to keep it out of server logs. The `/tts/api/ack` and
  `/alerts/api/ack` routes accept POST only.
- **Resource uploads use an allowlist** (documents, images, audio/video,
  archives; `VOD_FILES_ALLOWED_EXTENSIONS` overrides it). Existing files with
  other extensions are no longer served but can still be deleted.
- **Docker programs run as an unprivileged user** (`APP_UID`, default 1000).
  `./data` must be writable by that UID: `sudo chown -R 1000:1000 data`.
- **`/metrics` needs an admin session or `METRICS_TOKEN`**; other signed-in
  users get HTTP 403. `/healthz` shows disk data to admins only.
- **`streamvault stop|restart|update` exit with status 1** while recordings
  are running or when the archive cannot be scanned; `--force` overrides.
  `install.sh` exits with status 1 when it cannot scan the archive
  (`STREAMVAULT_FORCE_RESTART=1` overrides).
- **OBS overlay URLs:** `/tts/overlay` and `/alerts/overlay` load without a
  token. Pass the token in the URL fragment, for example
  `https://example.com/alerts/overlay#token=SECRET`; browsers never send the
  fragment to the server. The overlay APIs still require the token, as a
  header unless `OVERLAY_ALLOW_QUERY_TOKEN=true`.
- **The live watch page no longer embeds the Twitch player by default.**
  Every device plays the server stream (no Twitch ads, viewer IP addresses
  stay away from Twitch); `?embed=`/`?proxy=` have no effect and the CSP no
  longer allows Twitch frames. `LIVE_TWITCH_EMBED=true` restores the previous
  behaviour (Docker: set it in `.env`).
- **nginx: direct live routes need new locations.** Update your site from
  `nginx.conf` / INSTALL.md section 6 (`/_live_auth`, `/live/<channel>.ts`,
  `/live/<channel>/{stream,index}.m3u8`, `seg_N.ts`); without them the
  desktop real-time player cannot start.

### Security

- Cancelling a transcription only signals a verified `run_transcribe.py`
  process group, never PID 1 or the web worker's own group.
- The Twitch OAuth token never falls back to the command line when its
  private config file cannot be written.
- Login throttling counts only form submissions and groups IPv6 clients by
  /64. Failed logins are counted per account and network
  (`VOD_LOGIN_USER_MAX_FAILS`, default 10 per hour); an account-wide safety
  net (`VOD_LOGIN_USER_GLOBAL_MAX_FAILS`, default 100) only slows networks
  that themselves failed at least `VOD_LOGIN_USER_NET_MIN_FAILS` times
  (default 3), so the owner, even after a typo, is never locked out by
  failures produced elsewhere. Throttled requests get HTTP 429 with
  `Retry-After`. Known limit: an attacker with many fresh networks (e.g. the
  /64s of one /48) still gets that many guesses per network and hour; see
  SECURITY.md.
- The overlay pages are sent with `Referrer-Policy: no-referrer` and
  `Cache-Control: no-store`.
- Session revocation uses a random per-account token instead of a counter.
- Shared and downloaded resource files are always sent as
  `attachment`/`application/octet-stream` with `nosniff`; symlinked media
  and resource files are refused.
- Constant-time comparison for the environment admin password; unknown,
  inactive and environment accounts all pay the same password-hash cost.
  Client values in VOD debug logs are stripped of control characters.
- Live-only accounts can no longer open the live clip editor.
- The direct live routes (`/live/<channel>.ts`, LL-HLS playlist and
  segments) are served by nginx only after `auth_request /_live_auth`
  → `GET /api/live/auth` (session plus channel visibility; 401/403
  otherwise). The auth endpoint is exempt from rate limiting, since one
  viewer causes about one subrequest per second.
- The nginx access log uses the `timed` format (no cookies, adds request and
  upstream time).

### Added

- `scripts/vod_retention.py`: deletes local archive VODs older than
  `--days N` (dry run unless `--enforce`). Deletion uses the WebUI delete path
  (tombstone, thumbnail, HLS, chapters, shares) and removes the VOD's metadata
  and chat sidecars; folders with an active recording or remux are skipped.
  Optional offload channels (`--offload-slugs`) are only deleted when listed
  in an offload manifest. Example systemd service and timer in
  `scripts/systemd/`; see INSTALL.md, "VOD retention".

### Changed

- **S2, app factory and blueprints:** `create_app()` builds the web app;
  routes are grouped into 12 blueprints whose endpoint names are unchanged
  (`url_for` keeps working; the URL map is identical). Caches shared between
  workers go through `core.cache` (Redis in production).
- **S4, recording loop:** the recorder's decisions live in
  `recorder/recording_policy.py` (`RecordingPolicy`); processes start through
  a `ProcessRunner` (`recorder/recording_runtime.py`). Every child now starts
  in its own session (`start_new_session=True`), replacing the former
  `preexec_fn=os.setsid`.
- The "are recordings running?" check is shared in
  `scripts/recording-guard.sh`; the VPS deploy snapshot uses the same exclude
  list as the code copy (`scripts/deploy-excludes.txt`).
- `install.sh` aborts on a failing `nginx -t`, copies without databases,
  `data/`, `demo-data/` or `.env` (shared list in
  `scripts/deploy-excludes.txt`), runs git as the service user, keeps the
  initial password out of the terminal and does not restart services while
  recordings are running. `streamvault restart` accepts a single service.
- `scripts/streamvault_vps.sh deploy` takes a `sqlite3 .backup` of the
  database and checks it with `PRAGMA integrity_check` before touching the
  code.
- The nginx example sets `Connection` via `map`, keeps security headers in
  every location with its own `add_header`, marks protected thumbnails
  `private` and compresses proxied CSS/JS (`gzip_proxied any`).
- `core.channels` holds the channel helpers shared by the web UI and the
  recorder; `core.database` no longer depends on Flask.

### Removed

- **Whisper transcription.** The transcript button and panel on the VOD page,
  the `/vod/transcribe*` and `/vod/transcript/*` routes, `run_transcribe.py`,
  the worker's ASR pool and the automatic transcription after each recording
  are gone. A `transcribe` task still queued by an older release is failed
  once with a log line and never retried. Existing `*.transcript.txt` files
  stay on disk and can be deleted by hand.
  Operators: drop `TRANSCRIBE_MODEL`, `TRANSCRIBE_THREADS`,
  `TRANSCRIBE_LANGUAGE`, `TRANSCRIBE_BACKEND`, `WORKER_TRANSCRIBE_THREADS`,
  `TASK_MAX_ATTEMPTS_TRANSCRIBE` and `STREAM_RECORDER_AUTO_TRANSCRIBE` from
  the environment. The extras `transcribe` / `transcribe-fast` no longer
  exist; `openai-whisper`, `faster-whisper`, `ctranslate2` and `torch` can be
  uninstalled from the virtualenv (`pip uninstall openai-whisper
  faster-whisper ctranslate2 torch`) if nothing else needs them.
- **Text-to-speech overlay.** `/tts/overlay`, `/tts/api/*`, `/tts/audio/*`,
  the `!tts` chat bot (`core/tts_bot.py`, `core/tts_service.py`) and the
  `X-TTS-Token` header are gone. Operators: drop `TTS_ENABLED`,
  `TTS_MAX_CHARS`, `TTS_COOLDOWN_SEC`, `TTS_MAX_QUEUE`, `TTS_DEFAULT_VOICE`,
  `TTS_MOD_USERS`, `TWITCH_TTS_REWARD_IDS` and `TTS_OVERLAY_TOKEN_<CHANNEL>`;
  `edge-tts` can be uninstalled. Remove the TTS browser source from OBS.
  The alert overlay (`/alerts/*`) stays; its token is now `ALERT_OVERLAY_TOKEN`.
  `TTS_OVERLAY_TOKEN` is still read as a fallback and logs a deprecation
  warning; rename it.
- **Leftovers of the WoW stalker module** (CSS for `page-stalk*`, sidebar
  endpoints, the `/admin/stalk` guard and unused template constants).

### Fixed

- No broad exception handler that only passes, continues or returns a
  constant without logging is left (database layer, recorder, worker, clip
  engine, web UI, storage providers, API clients). The expected failures are
  caught by class and logged, and fallback values are unchanged. Operator
  impact:
  - An unexpected error class (for example a `TypeError` from a code bug) is
    no longer hidden. It now shows up as an HTTP 500 or a traceback in the
    log.
  - Database fallbacks catch `sqlite3.Error`, `OSError` and `ValueError`
    (`core.database.DB_ACCESS_ERRORS`). Warnings from per-request lookups
    are limited to one per minute and call site.
  - The recording loop's own checks stay broad on purpose, so they can never
    stop a recording, but they now log a throttled warning.
  - Broad handlers that do something other than these three patterns still
    exist and log nothing.
- A deleted VOD or clip whose file was still on disk opened in the player:
  the tombstone check's `abort(404)` was swallowed by its own error handler.
- A fresh database no longer logs a bogus `users.role` migration warning.
- Desktop live player: `/live/<channel>.ts` was not routed by nginx and
  ended in a redirect to the watch page. mpegts.js now gets an absolute URL
  (its blob: worker has no base URL: "Failed to parse URL from
  /live/<channel>.ts") and switches to LL-HLS after two failed attempts
  before the first frame, because the MPEG-TS fan-out is an optional
  external livebot. Native HLS (iOS/Safari) plays the session-checked
  `/live/playlist/<channel>`.

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
- **Content-Security-Policy is enforced.** `VOD_CSP_ENFORCE` now defaults to
  `true`: inline scripts need the per-request nonce and inline event handlers
  (`onclick=` ...) are blocked. Custom templates or overlays with inline
  scripts must use `nonce="{{ csp_nonce() }}"`. `VOD_CSP_ENFORCE=false` falls
  back to `Content-Security-Policy-Report-Only`.
- **Overlay tokens as headers.** The TTS and alert overlays read the token
  from `#token=` (or `?token=`) of their own URL and send it to the API only as
  `X-TTS-Token` / `X-Alert-Token`; audio is fetched with the header and played
  from a blob URL. Options > Settings shows the recommended OBS URLs.

### Security

- Client IPs behind a proxy come from `X-Real-IP` or the right-most
  `X-Forwarded-For` hop; rotating `X-Forwarded-For` no longer bypasses login
  rate limits and bans. `VOD_TRUSTED_PROXIES` accepts CIDR networks.
- Code and dot-directories below `VOD_BASE_DIR` can no longer be deleted as
  streamer archives.
- WebUI: chat fallbacks escape Twitch chat text, toasts use `textContent`,
  banner/hero URLs only reach CSS `url()` through the `css_url` filter, and
  the CSP allows the emote hosts (BTTV, FFZ, 7TV, ivr.fi) and `https:` images.

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

- WebUI accessibility and design (audit 2026-11-12): native `<dialog>` modals
  with focus handling, one confirmation dialog (`data-confirm`) for restart,
  maintenance and deletions, bottom navigation with logout below 768px,
  "Log out everywhere" in the rail, bottom navigation and mobile pages,
  skip link, labelled fields, AA contrast tokens, unlocked zoom on mobile.
  `static/css/tokens.css` is the single token layer; stylesheets load as
  parallel `<link>`s.

- GitHub Actions CI (Python 3.10–3.12, ruff, unit tests, shell syntax),
  Dependabot, contribution and security guidelines.
- Web UI in 12 languages, chosen from the browser language or the switcher.
- `scripts/demo_seed.py` creates a sample archive for trying StreamVault
  without recording anything.
