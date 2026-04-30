# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.0] — 2026-04-30

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

[Unreleased]: https://github.com/luisesk/dubsmith/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/luisesk/dubsmith/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/luisesk/dubsmith/releases/tag/v0.6.0
