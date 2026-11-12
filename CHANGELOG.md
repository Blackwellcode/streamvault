# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
- **Overlay tokens as headers.** The TTS and alert overlays read `?token=`
  from their own URL but send it to the API only as `X-TTS-Token` /
  `X-Alert-Token`; audio is fetched with the header and played from a blob URL.

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
  skip link, labelled fields, AA contrast tokens, unlocked zoom on mobile.
  `static/css/tokens.css` is the single token layer; stylesheets load as
  parallel `<link>`s.

- GitHub Actions CI (Python 3.10–3.12, ruff, unit tests, shell syntax),
  Dependabot, contribution and security guidelines.
- Web UI in 12 languages, chosen from the browser language or the switcher.
- `scripts/demo_seed.py` creates a sample archive for trying StreamVault
  without recording anything.
