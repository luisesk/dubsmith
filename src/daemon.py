"""Long-running daemon: FastAPI server + APScheduler periodic scans + queue worker."""
import logging
import threading
import time

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

from . import config, health, logbuf, reconcile, scanner, staging
from .api import make_app
from .queue import Queue
from .settings_store import SettingsStore
from .shows import ShowsStore
from .sonarr import Sonarr
from .sources import SourcesStore
from .users import UsersStore
from .worker import Worker

log = logging.getLogger(__name__)


def _scan_all(cfg: dict, queue: Queue, shows: ShowsStore) -> None:
    """Scan every enabled tracked show, enqueue missing-dub eps."""
    sonarr = Sonarr(cfg["sonarr"]["url"], cfg["sonarr"]["api_key"])
    sonarr_prefix = (cfg.get("paths_extra") or {}).get("sonarr_prefix", "/downloads")
    default_lang = cfg["target_language"]["audio"]
    tracked = shows.load()
    n = 0
    for sid_raw, show in tracked.items():
        if not show.get("enabled", True):
            continue
        sid = int(sid_raw)
        cr_seasons = show.get("cr_seasons") or {}
        target_lang = show.get("target_audio") or default_lang
        try:
            missing = scanner.find_missing(
                sonarr, sid, target_lang,
                path_remap=(sonarr_prefix, cfg["paths"]["library_in_container"]),
            )
        except Exception as e:
            log.warning("scan failed for series %s: %s", sid, e)
            continue
        for m in missing:
            if str(m.season) not in cr_seasons:
                continue  # silently skip seasons without CR mapping
            queue.upsert_pending(m.series_id, m.season, m.episode, m.target_path)
            n += 1
    log.info("scan_all: enqueued/refreshed %d job rows (tracked=%d)", n, len(tracked))


def _retry_failed(queue: Queue, max_attempts: int) -> None:
    n = queue.retry_failed(max_attempts=max_attempts)
    if n:
        log.info("retry_failed: %d jobs reset to pending", n)


def _worker_loop(name: str, cfg: dict, queue: Queue, shows: ShowsStore, settings: SettingsStore, stop: threading.Event) -> None:
    worker = Worker(cfg, queue, shows, settings=settings)
    while not stop.is_set():
        job = queue.claim_next()
        if not job:
            stop.wait(timeout=10)
            continue
        log.info("[%s] picked job %d", name, job.id)
        try:
            worker.process(job)
        except Exception as e:
            log.exception("[%s] crashed on job %s: %s", name, job.id, e)
            queue.set_state(job.id, "failed", last_error=f"worker exception: {e}")


def run() -> None:
    import os as _os
    level_name = _os.environ.get("DUBSMITH_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # add ring-buffer handler in addition to default stderr stream
    logbuf.install(capacity=2000)
    log.info("loading config")
    # ensure /data subdirs exist (idempotent; works under any container uid)
    data = config.data_dir()
    for sub in ("staging", "widevine", "mdnx/install-config", "mdnx/config", "_home"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    cfg = config.load()
    log.info("config loaded; opening queue")
    queue = Queue(config.data_dir() / "queue.db")
    n_reset = queue.reset_stale_running()
    if n_reset:
        log.warning("reset %d stale running jobs back to pending (likely from prior restart)", n_reset)
    log.info("queue open; loading shows")
    shows = ShowsStore(config.data_dir() / "shows.yml")
    log.info("shows loaded; loading sources")
    sources = SourcesStore(config.data_dir() / "sources.yml")
    log.info("sources loaded; loading settings")
    settings = SettingsStore(config.data_dir() / "settings.yml")
    users = UsersStore(config.data_dir() / "users.yml")
    # bootstrap admin from cfg.api on first run
    if not users.load():
        u = (cfg.get("api") or {}).get("user", "admin")
        p = (cfg.get("api") or {}).get("password", "admin")
        users.bootstrap(u, p)
        log.info("bootstrapped initial user '%s' (admin)", u)
    log.info("settings + users loaded")

    # one-time bootstrap: import legacy cfg["shows"] into shows.yml if empty
    if not shows.load() and cfg.get("shows"):
        for sid, sh in cfg["shows"].items():
            shows.upsert(int(sid), **sh)

    stop = threading.Event()
    # Single sequential worker: mdnx has known parallelism bugs (ENOENT on temp-file
    # rename, config-race during boot). Sequential keeps things robust; a download +
    # sync + mux cycle is ~2-3 min anyway.
    log.info("starting worker thread")
    threading.Thread(
        target=_worker_loop,
        args=("w1", cfg, queue, shows, settings, stop),
        daemon=True, name="worker-1",
    ).start()

    # Startup janitor: nuke episode dirs older than 7 days (or what config says)
    staging_root = cfg["paths"]["staging"]
    janitor_age_days = float(cfg.get("scheduler", {}).get("staging_max_age_days", 7))
    try:
        n = staging.sweep_old(staging_root, max_age_days=janitor_age_days)
        if n:
            log.info("startup janitor: removed %d stale staging dirs (>%.1fd)", n, janitor_age_days)
        usage = staging.staging_disk_usage(staging_root)
        log.info("staging usage: %.1f MB across %d episode dirs",
                 usage["bytes"] / 1024 / 1024, usage["episode_dirs"])
    except Exception as e:
        log.warning("startup janitor failed: %s", e)

    sched = BackgroundScheduler(daemon=True)
    sched.add_job(
        lambda: _scan_all(cfg, queue, shows),
        "interval",
        hours=cfg.get("scheduler", {}).get("scan_interval_hours", 6),
        next_run_time=None,
    )
    sched.add_job(
        lambda: _retry_failed(queue, cfg.get("scheduler", {}).get("max_attempts", 3)),
        "interval",
        hours=cfg.get("scheduler", {}).get("retry_interval_hours", 1),
        next_run_time=None,
    )
    sched.add_job(
        lambda: staging.sweep_old(staging_root, max_age_days=janitor_age_days),
        "interval",
        hours=cfg.get("scheduler", {}).get("janitor_interval_hours", 12),
        next_run_time=None,
    )
    sched.add_job(
        lambda: health.run_all_checks(sources),
        "interval",
        minutes=cfg.get("scheduler", {}).get("health_interval_minutes", 30),
        next_run_time=None,
    )

    def _reconcile():
        sonarr = Sonarr(cfg["sonarr"]["url"], cfg["sonarr"]["api_key"])
        try:
            reconcile.run(sonarr, shows, queue)
        except Exception as e:
            log.warning("scheduled reconcile failed: %s", e)

    sched.add_job(
        _reconcile,
        "interval",
        hours=cfg.get("scheduler", {}).get("reconcile_interval_hours", 24),
        next_run_time=None,
    )
    sched.start()
    # Initial health probe in background so dashboard reflects state on first load
    threading.Thread(target=lambda: health.run_all_checks(sources), daemon=True).start()

    # initial scan on startup
    threading.Thread(target=lambda: _scan_all(cfg, queue, shows), daemon=True).start()

    app = make_app(cfg, queue, shows, sources, settings, users)
    port = int(cfg.get("api", {}).get("port", 8080))
    log.info("api on :%d", port)
    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    finally:
        stop.set()
        sched.shutdown(wait=False)


if __name__ == "__main__":
    run()
