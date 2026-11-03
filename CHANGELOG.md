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

### Added

- GitHub Actions CI (Python 3.10–3.12, ruff, unit tests, shell syntax),
  Dependabot, contribution and security guidelines.
