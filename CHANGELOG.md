# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.10.1] — 2026-04-30

### Added
- **Untracked shows now get source-probed too**: discover scan calls `aniDL --service crunchy --search <title>` for every untracked Sonarr series missing the dub locally, parses the top match, reports availability. Lets you spot shows worth setting up that you don't even track yet.
- Untracked rows show the matched CR title (so you can verify the auto-match before clicking ⚙ setup).
- **Parallel scan via ThreadPoolExecutor** with default 4 workers. Tunable via `scheduler.discover_workers` in `config.yml`. Bounded to keep CR rate-limit happy and leave room for the worker pipeline.
- **Incremental save** every 25 rows: interrupted scans (container restart, tab close) keep their partial cache.
- **Live progress** ("scanning… 47/351") via new `progress.done/total` field on `/api/discover`.

### Notes on safety
- Discover probes are read-only mdnx calls; they don't write `dir-path.yml` like the worker pipeline does. The boot lock that protects worker mdnx config is unaffected by parallel discover probes.
- Each ThreadPoolExecutor worker spawns its own subprocess; mdnx subprocesses are independent. Concurrent token refreshes are mdnx-internal and use file locks.

[0.10.1]: https://github.com/luisesk/dubsmith/compare/v0.10.0...v0.10.1

## [0.10.0] — 2026-04-30

### Changed
- **Discover page now answers the right question**: \"Which library shows are missing the dub AND have it available on the connected source?\" Previous version only probed local files. Now, for tracked shows missing the dub locally, we additionally call mdnx (`aniDL --service <kind> -s <cr_season_id>`) per mapped season to read the **available dub languages** on the source. Cross-form lang match (`pt`/`por`/`pt-BR`) decides if the target is reachable.
- New default filter chip: **Actionable** (missing locally + source has dub). The dataset that's actually worth bulk-enqueuing.
- Stats card "Actionable" replaces "Series in Sonarr" as the headline metric.
- Per-row checkbox auto-disables on non-actionable rows so bulk-scan only acts on the relevant set.
- Source column shows source kind + which seasons have the dub. \"no dub\" / \"untracked\" / \"probe err\" states distinguished from each other.
- Added filter chips: \"Missing — source lacks dub\", \"Missing — needs setup\".

### Notes
- Untracked shows skip the source probe (would need a full `--search` per show, which is too slow for v1). Their bucket is \"needs setup\" — wire CR mapping via the existing wizard, then re-scan.
- Source probe is the slow part: ~3s per mapped CR season. With 4 tracked shows × 1-2 seasons each it's ~15s. Cached for the next page load.

[0.10.0]: https://github.com/luisesk/dubsmith/compare/v0.9.1...v0.10.0

## [0.9.1] — 2026-04-30

### Fixed
- **Discover scan state survives page reload.** Button reflects actual server state via `/api/discover.running`. If you reload mid-scan the button stays `Scanning…` and disabled, and JS auto-attaches to the in-flight scan via the existing 2s poll. New scans rejected (with toast) while one is running — no double-trigger.

[0.9.1]: https://github.com/luisesk/dubsmith/compare/v0.9.0...v0.9.1

## [0.9.0] — 2026-04-30

### Added
- **Discover page** (`/discover`): library-wide audio coverage scanner.
  - Walks every Sonarr series, ffprobes one sample episode per show, classifies as `has dub` / `missing dub` / `error`. Cross-form lang matching (`pt`/`por`/`pt-BR`).
  - Filter chips: Missing dub (default), All, Has dub, Tracked only, Untracked only, Probe errors. Plus title search.
  - Multi-select rows + bulk actions: **Scan selected (tracked)** kicks off the existing per-series scan for each picked tracked show, enqueues all missing eps for which a CR mapping exists. Untracked shows route to the existing setup wizard via the row's "⚙ setup" link.
  - **Scan now** button kicks a background scan thread; progress polls every 2s. Results persist to `data/discover.json` so reloads are instant.
  - Stats cards: total / missing / has dub / tracked-vs-untracked + probe errors.
- API: `GET /api/discover` (cached results), `POST /api/discover/scan` (operator), `POST /api/discover/bulk-scan` (operator) with audited bulk enqueue.
- Sidebar nav gets a Discover entry.
- Tests: +9 cases (classification + cache + cross-form lang). 89 total.

[0.9.0]: https://github.com/luisesk/dubsmith/compare/v0.8.5...v0.9.0

## [0.8.5] — 2026-04-30

### Fixed
- **Worker burned bandwidth on doomed jobs**: when path remap was misconfigured (`paths_extra.sonarr_prefix` vs `paths.library_in_container` drift), the worker would download 1+ GB from CR before sync ran ffprobe on a non-existent target and failed with a generic error. Worker now does a pre-flight `Path(job.target_path).exists()` check and fails fast with a clear remap-hint message — saving the round trip.
- **Opaque ffprobe errors**: probe.streams() was using `check=True` which raised `CalledProcessError` whose `__str__` is just `"non-zero exit status 1"`. Now captures stderr explicitly and raises `RuntimeError(f"ffprobe failed on <path>: <last stderr line>")`. Same for invalid-JSON output.

[0.8.5]: https://github.com/luisesk/dubsmith/compare/v0.8.4...v0.8.5

## [0.8.4] — 2026-04-30

### Fixed
- **Mobile layout** (≤768px) across all pages, no desktop changes:
  - Side-by-side card grids (dashboard "Now muxing" + "Recent activity", queue jobs + log) now stack vertically.
  - Library + Shows poster grids switch to **2 columns** instead of full-width single column.
  - Page headers stack title + action buttons; no more horizontal overflow.
  - Settings form rows (label + input + save button) stack with label above input.
  - Shows table hides STATS column ≤768px and CR SEASONS column ≤480px to fit narrow screens.
  - Alert + user dropdowns sized to viewport width (no clipping).
  - Tighter card + page padding on mobile.

[0.8.4]: https://github.com/luisesk/dubsmith/compare/v0.8.3...v0.8.4

## [0.8.3] — 2026-04-30

### Removed
- **Concurrency settings (downloads/muxes/sync)**: only `concurrency.downloads` was ever wired (and even that drove the *whole* pipeline-worker count, not download-phase parallelism). `muxes` and `sync` were UI-only lies. mdnx has known parallelism bugs (config race + ENOENT on temp-file rename), so single-worker is more reliable. The dashboard "X / 4" widget that hardcoded the cap is gone too.

### Fixed
- **ENOENT race during mdnx finalization**: downloader now `rmtree`-s the per-episode out_dir before each attempt (was only globbing `temp-*.m4s`). Stops mdnx from confusing leftover state with current.
- Concurrency widget on queue page that hardcoded `/ 4` and was always wrong: removed entirely.

### Added
- **Alert system**:
  - `src/alerts.py` in-memory store (key/severity/title/message/actions).
  - `src/health.py` periodic mdnx whoami probe; raises `source.crunchyroll.auth` alert when CR session expires (parses `USER: Anonymous`). Clears it on recovery. Schedule: every `scheduler.health_interval_minutes` (default 30).
  - Worker raises `source.crunchyroll.selection` warning when mdnx returns `Episodes not selected!`.
  - `GET /api/alerts` (auth) lists; `DELETE /api/alerts/{key}` (operator) dismisses.
  - **Topbar bell icon** with red badge + dropdown listing alerts. Polls every 60s. Reconnect actions link to settings.
- **`POST /api/restart`** (admin): SIGTERM the daemon; container restart-policy=unless-stopped brings it back. Lets you apply settings changes without shell access.

[0.8.3]: https://github.com/luisesk/dubsmith/compare/v0.8.2...v0.8.3

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
