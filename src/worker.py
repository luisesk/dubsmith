"""Pipeline worker: process one queued job end-to-end."""
import logging
from pathlib import Path

from . import health, mux, notify, probe, staging, sync
from .downloader import MdnxDownloader
from .queue import Job, Queue
from .shows import ShowsStore
from .sonarr import Sonarr

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, cfg: dict, queue: Queue, shows: ShowsStore, settings=None):
        self.cfg = cfg
        self.queue = queue
        self.shows = shows
        self.settings = settings
        self.sonarr = Sonarr(cfg["sonarr"]["url"], cfg["sonarr"]["api_key"])
        # default downloader; per-show overrides applied at process()
        self.dl = MdnxDownloader(
            staging_dir=cfg["paths"]["staging"],
            widevine_dir=cfg["widevine_dir"],
            dub_lang=cfg["target_language"]["cr_dub_lang"],
            sub_lang=cfg["target_language"]["cr_sub_lang"],
        )

    def process(self, job: Job) -> None:
        cfg = self.cfg
        show = self.shows.get(job.series_id) or cfg.get("shows", {}).get(job.series_id) or cfg.get("shows", {}).get(str(job.series_id))
        if not show:
            self.queue.set_state(job.id, "failed", last_error=f"no show config for series {job.series_id}")
            return

        cr_seasons = show.get("cr_seasons", {})
        season_offset = show.get("season_offset", {})
        cr_season_id = cr_seasons.get(str(job.season))
        if not cr_season_id:
            self.queue.set_state(job.id, "failed", last_error=f"no cr_seasons mapping for S{job.season}")
            return
        cr_ep = job.episode + season_offset.get(str(job.season), 0)

        # Pre-flight: target file must exist inside the container before we burn
        # bandwidth on a 1+ GB mdnx download. Catches path-remap misconfigs
        # (sonarr_prefix vs library_in_container drift) early with a clear msg.
        if not Path(job.target_path).exists():
            self.queue.set_state(
                job.id, "failed",
                last_error=(f"target file not found: {job.target_path} — check "
                            f"paths.library_in_container vs paths_extra.sonarr_prefix "
                            f"in config.yml"),
            )
            return

        log.info("=== job %d: S%02dE%02d -> CR season %s ep %d ===",
                 job.id, job.season, job.episode, cr_season_id, cr_ep)

        # per-show language + source override
        dub = show.get("cr_dub_lang") or cfg["target_language"]["cr_dub_lang"]
        sub = show.get("cr_sub_lang") or cfg["target_language"]["cr_sub_lang"]
        self.dl.dub_lang = dub
        self.dl.sub_lang = sub
        self.dl.source = show.get("source", "crunchyroll")

        # progress callback writes to queue; throttled to ~1/s by sqlite cost
        last_t = [0.0]
        import time as _t
        def on_prog(pct, phase, bd, bt):
            now = _t.time()
            if now - last_t[0] < 0.7 and pct is not None:
                return
            last_t[0] = now
            self.queue.update_progress(job.id, progress=pct, phase=phase,
                                       bytes_done=bd, bytes_total=bt)

        staging_root = cfg["paths"]["staging"]
        def _clean():
            try:
                staging.clean_episode(staging_root, cr_season_id, job.season, cr_ep)
            except Exception as e:
                log.warning("staging cleanup failed: %s", e)

        self.queue.update_progress(job.id, progress=0.0, phase="starting download")
        try:
            src_path = self.dl.download_audio(cr_season_id, cr_ep, job.season, on_progress=on_prog)
        except Exception as e:
            err = str(e)
            self.queue.set_state(job.id, "failed", last_error=f"download: {err}")
            # Specific error pattern: mdnx couldn't pick the episode — usually stale token
            if "Episodes not selected" in err or "Anonymous" in err:
                health.report_episodes_not_selected()
            _clean()
            return

        # capture downloaded size
        try:
            size = src_path.stat().st_size
            self.queue.update_progress(job.id, progress=1.0, phase="downloaded",
                                       bytes_done=size, bytes_total=size)
        except Exception:
            pass

        # If operator supplied a manual delay, skip detection entirely.
        if job.manual_delay_ms is not None:
            log.info("job %d using manual delay=%dms (skipping sync detect)", job.id, job.manual_delay_ms)
            self.queue.set_state(job.id, "muxing",
                                 sync_delay_ms=job.manual_delay_ms,
                                 sync_score=999.0)  # 999 = sentinel for manual
            result_delay = job.manual_delay_ms
        else:
            self.queue.set_state(job.id, "syncing")
            self.queue.update_progress(job.id, progress=0.0, phase="cross-correlating")
            try:
                jpn_idx = probe.jpn_audio_index(job.target_path)
                sync_cfg = cfg["sync"]
                result = sync.detect(
                    job.target_path, jpn_idx, str(src_path),
                    trim_s=sync_cfg["trim_seconds"],
                    bound_s=sync_cfg["bound_seconds"],
                )
            except Exception as e:
                self.queue.set_state(job.id, "failed", last_error=f"sync: {e}")
                _clean()
                return

            log.info("sync delay=%dms score=%.2f", result.delay_ms, result.score)

            if result.score < cfg["sync"]["min_score"]:
                self.queue.set_state(
                    job.id, "quarantined",
                    sync_delay_ms=result.delay_ms, sync_score=result.score,
                    last_error=f"low confidence ({result.score:.2f}) — set manual delay if needed",
                )
                _clean()
                return
            if abs(result.delay_ms) > cfg["sync"]["max_abs_delay_ms"]:
                self.queue.set_state(
                    job.id, "quarantined",
                    sync_delay_ms=result.delay_ms, sync_score=result.score,
                    last_error=f"delay {result.delay_ms}ms out of range — set manual delay if needed",
                )
                _clean()
                return

            self.queue.set_state(
                job.id, "muxing",
                sync_delay_ms=result.delay_ms, sync_score=result.score,
            )
            result_delay = result.delay_ms
        self.queue.update_progress(job.id, progress=0.5, phase="mkvmerge")
        try:
            audio_lang = show.get("target_audio") or cfg["target_language"]["audio"]
            audio_label = show.get("target_audio_label") or cfg["target_language"]["audio_label"]
            # Default mux workdir to /data/mux (subdir of staging volume — fast
            # local disk on most setups). Can be overridden via paths.mux_workdir.
            mux_workdir = (cfg.get("paths") or {}).get(
                "mux_workdir") or str(Path(cfg["paths"]["staging"]).parent / "mux")
            mux.inject(
                job.target_path, str(src_path), result_delay,
                lang=audio_lang, track_name=audio_label,
                mux_workdir=mux_workdir,
            )
        except Exception as e:
            self.queue.set_state(job.id, "failed", last_error=f"mux: {e}")
            _clean()
            return

        # Success: nuke the per-episode staging dir + prune empty parents
        _clean()
        self.queue.set_state(job.id, "done")
        log.info("done: job %d", job.id)
        # trigger Sonarr rescan so DB picks up new filename
        settings_data = self.settings.load() if self.settings else {}
        sonarr_cfg = settings_data.get("sonarr", {})
        if sonarr_cfg.get("rescan_after_mux", True):
            try:
                self.sonarr.rescan_series(job.series_id)
            except Exception as e:
                log.warning("sonarr rescan failed: %s", e)
        # trigger Plex/Jellyfin library refresh
        ls_cfg = settings_data.get("library_server") or {}
        if ls_cfg.get("url") and ls_cfg.get("token"):
            try:
                from .library_server import LibraryServer
                ls = LibraryServer(ls_cfg.get("type", "plex"), ls_cfg["url"],
                                   ls_cfg["token"], ls_cfg.get("section_id"))
                ok = ls.refresh_section()
                log.info("library refresh (%s): %s", ls_cfg.get("type", "plex"), "ok" if ok else "fail")
            except Exception as e:
                log.warning("library refresh failed: %s", e)
        # optional: unmonitor episode in Sonarr to prevent re-grab overwriting our muxed file
        if self.settings and self.settings.load().get("sonarr", {}).get("unmonitor_after_mux"):
            try:
                ep_id = self.sonarr.find_episode_id(job.series_id, job.season, job.episode)
                if ep_id:
                    self.sonarr.unmonitor_episode(ep_id)
                    log.info("unmonitored sonarr ep %d (S%02dE%02d)", ep_id, job.season, job.episode)
            except Exception as e:
                log.warning("sonarr unmonitor failed: %s", e)
        # ntfy push (best-effort)
        nt = cfg.get("ntfy") or {}
        if nt.get("url") and nt.get("topic"):
            notify.ntfy(
                nt["url"], nt["topic"],
                f"{show.get('name','?')} S{job.season:02d}E{job.episode:02d} dub injected (delay {result.delay_ms}ms)",
                title="plex-dub",
                token=nt.get("token"),
            )
