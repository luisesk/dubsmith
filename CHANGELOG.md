# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.8.2] — 2026-04-30

### Fixed
- **Staging dir leak**: worker only `unlink()`-ed the muxed `.mkv` — mdnx leaves dozens of `temp-*.m4s`, fonts, .nfo, and .mp4 fragments per episode. Failed/quarantined jobs left everything. Sandbox staging had grown to 5.7 GB. Now `staging.clean_episode` rmtrees the entire per-episode dir on every terminal state (done, failed, quarantined) and prunes empty parent dirs (S## and CR-id).

### Added
- **Startup janitor**: on daemon boot, sweep episode dirs older than `scheduler.staging_max_age_days` (default 7). Re-runs every `scheduler.janitor_interval_hours` (default 12). Protects against crash-orphaned dirs.
- **`GET /api/staging`**: reports `bytes` + `episode_dirs` count for monitoring.
- **`POST /api/staging/sweep`** (admin): manually purge episode dirs older than supplied `max_age_days` (0 = nuke all). Audited.

[0.8.2]: https://github.com/luisesk/dubsmith/compare/v0.8.1...v0.8.2

## [0.8.1] — 2026-04-30

### Changed
- **Topbar user menu** replaces the bottom-of-sidebar Profile/Logout text links. Avatar button (top-right) opens a dropdown with Profile + Sign out, properly iconified.

### Added
- **Avatar upload** at `/profile`: PNG/JPEG/WebP/GIF up to 2 MB. Stored at `/data/avatars/{username}.{ext}`. Cleared via Remove button or `DELETE /api/users/me/avatar`. Audited.
- `GET /api/users/{username}/avatar` serves the file (cache 5 min); falls back to initial-letter circle on 404.

[0.8.1]: https://github.com/luisesk/dubsmith/compare/v0.8.0...v0.8.1

## [0.8.0] — 2026-04-30

### Fixed
- **`mux.py` was hardcoded to pt-BR**: filename suffix and audio-track strip now derived from the target `lang` arg via `_LANG_SUFFIX` map + `lang_matches`. Other target languages no longer mis-mux.
- **Lossless mux when `delay_ms ≥ 0`**: switched from blanket ffmpeg re-encode to `mkvmerge --sync 0:N`. ffmpeg only used when negative delay requires PTS rewrite. Preserves original Crunchyroll audio bitrate.
- **`probe.por_audio_indices` was hardcoded to "por"**: replaced with `audio_indices(path, lang)` that normalizes ISO 639-1/2 codes.
- **Language code mismatch (`pt` vs `por` vs `pt-BR`)**: introduced `src/lang.py` with `normalize()` + `lang_matches()`. Wired into scanner + mux for cross-form comparison.
- **`api._proxy_image` ignored settings overrides**: now routes through `_sonarr_creds()` so settings UI controls poster/fanart proxy too.
- Dead `if False else None` artifact removed from `api.py`.

### Security
- **Rate-limit password change endpoints**: `/api/users/me/password` and `/api/users/{username}/password` now subject to a sliding-window lockout (5 failed attempts per 5 min → 15 min lockout, per IP+actor). Pairs with the existing login throttle.
- **Audit log**: append-only JSONL at `/data/audit.log`. Records: login (success/fail), user create/delete/role-change, password changes, settings updates, backup downloads, manual sync overrides. Plaintext secrets (api_key/token/password/webhook_secret) are scrubbed before logging. Admin can read via `GET /api/audit`.
- **Webhook payload size cap**: `/sonarr-webhook` rejects bodies over 256 KB before parsing (returns 413).
- **CSRF token on `/login` form**: per-session token issued on GET, validated on POST. Token rotates on successful auth.

### Added
- **Manual sync override**: operator can set a manual delay (ms) on a quarantined job via `POST /api/jobs/{id}/manual-delay`. Worker re-mounts the job, skips cross-correlation, mux with the supplied delay. Queue UI exposes a `↹ ms` button on quarantined rows. Required schema migration → `manual_delay_ms` column on `jobs` (schema v3).
- **ffprobe cache**: `probe.streams()` cached by `(path, mtime, size)` with 1h TTL + 5000-entry LRU. Speeds up large-library scans 10–100×. Bypassable via `no_cache=True`.
- **Prometheus `/metrics`**: plain-text exposition endpoint. Surfaces: jobs per state, done counter + 24h gauge, avg sync delay/score, build version. Unauthenticated by design — bind behind a reverse proxy ACL if exposing publicly.

### UI
- **Show card title clamp** (Library + Shows pages): titles clamp to exactly 2 lines with ellipsis. PT progress bars and action buttons now align horizontally regardless of title length.
- **Sidebar nav icons**: bumped 14 → 18px, brighter color, larger hit target (10px padding).
- **Theme toggle button**: icon now properly centered (was off due to inline span baseline).

[Unreleased]: https://github.com/luisesk/dubsmith/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/luisesk/dubsmith/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/luisesk/dubsmith/compare/v0.6.0...v0.7.0

### Added
- Multi-source download support: Crunchyroll, Hidive, AnimationDigitalNetwork (ADN). Per-show `source` field in `shows.yml` selects which mdnx service to use; auth flow already covered all three.
- `SERVICE_MAP` in `downloader.py` translating Dubsmith source keys → mdnx `--service` flag values.
- `source` query param on `/api/shows/search` and `/api/cr/season/{id}/dubs`; `source` field accepted by `/api/shows/quick-add`.

### Changed
- README expanded to advertise all three supported sources.
- Removed phase-tracking "Status" section from README (project is now beyond plan-tracking).

## [0.6.0] — 2026-04-30

Initial public release.

### Added
- End-to-end pipeline: Sonarr scan → mdnx download (DRM-decrypted with user-supplied Widevine CDM) → FFT cross-correlation sync → mkvmerge in-place remux → Sonarr rescan + library refresh.
- FastAPI web UI: dashboard, queue, library browser, per-show wizard, settings, logs.
- Multi-user auth with admin / operator / viewer roles, PBKDF2-SHA256 password hashing, login throttle, cookie sessions, HTTP Basic.
- Sonarr webhook with shared-secret header auth.
- Plex / Jellyfin library refresh via `LibraryServer` abstraction.
- `/health` + `/health/deep` endpoints; `/api/backup` admin-only tar.gz of `/data`.
- Mobile-responsive UI; collapsing sidebar below 768px.
- Env-var config overrides (`DUBSMITH_<SECTION>__<KEY>`); startup validation with default fallback.
- SQLite WAL queue with schema migrations + stale-job recovery on restart.
- APScheduler periodic scans + retry of failed jobs.
- Multi-worker concurrency with mdnx boot lock to avoid config races.
- Real-time progress via mdnx stdout parsing; phase regression detection.
- 44 unit tests across security, queue, users, settings, config, library_server, sync.
- Multi-arch (linux/amd64, linux/arm64) Docker image published to GHCR via GitHub Actions on push to main and tagged releases.

[Unreleased]: https://github.com/luisesk/dubsmith/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/luisesk/dubsmith/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/luisesk/dubsmith/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/luisesk/dubsmith/releases/tag/v0.6.0
