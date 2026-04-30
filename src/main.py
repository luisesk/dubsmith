"""Phase 1 MVP CLI: process one Sonarr series end-to-end."""
import logging
import sys

import click

from . import config, mux, notify, probe, scanner, sync
from .downloader import MdnxDownloader
from .library_server import LibraryServer
from .sonarr import Sonarr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("plex-dub")


def cr_episode_for(season: int, episode: int, season_offset: dict) -> int:
    """Convert Sonarr per-season episode number to CR absolute number using overrides."""
    return episode + season_offset.get(str(season), 0)


def process_series(cfg: dict, series_id: int, dry_run: bool = False) -> int:
    show = cfg["shows"].get(series_id) or cfg["shows"].get(str(series_id))
    if not show:
        log.error("series %d not in config.shows", series_id)
        return 1

    sonarr = Sonarr(cfg["sonarr"]["url"], cfg["sonarr"]["api_key"])
    # Library server (plex/jellyfin); legacy cfg["plex"] still supported for back-compat
    ls_cfg = cfg.get("library_server") or {}
    if ls_cfg.get("url"):
        plex = LibraryServer(ls_cfg.get("type", "plex"), ls_cfg["url"],
                             ls_cfg.get("token", ""), ls_cfg.get("section_id"))
    elif cfg.get("plex", {}).get("url"):
        plex = LibraryServer("plex", cfg["plex"]["url"], cfg["plex"]["token"],
                             cfg["plex"].get("library_section_id"))
    else:
        plex = None

    sonarr_root = sonarr.series(series_id)["path"]
    library_in_container = cfg["paths"]["library_in_container"]
    # Sonarr's path prefix may be e.g. /downloads/Videos/Animes/<series>; map to container /library/Videos/Animes/<series>
    # We assume Sonarr root_path /downloads -> /library here. Customize if needed.
    sonarr_prefix = "/downloads"

    target_audio = cfg["target_language"]["audio"]
    missing = scanner.find_missing(
        sonarr, series_id, target_audio,
        path_remap=(sonarr_prefix, library_in_container),
    )
    log.info("series %d: %d eps missing %s", series_id, len(missing), target_audio)
    if dry_run:
        for m in missing:
            log.info("would process S%02dE%02d: %s", m.season, m.episode, m.target_path)
        return 0

    dl = MdnxDownloader(
        staging_dir=cfg["paths"]["staging"],
        widevine_dir=cfg["widevine_dir"],
        dub_lang=cfg["target_language"]["cr_dub_lang"],
        sub_lang=cfg["target_language"]["cr_sub_lang"],
    )

    season_offset = show.get("season_offset", {})
    cr_seasons = show.get("cr_seasons", {})  # {"1": "GR49C7EPD", ...}
    sync_cfg = cfg["sync"]
    ok = 0
    failed = 0
    quarantined = 0
    for m in missing:
        cr_ep = cr_episode_for(m.season, m.episode, season_offset)
        cr_season_id = cr_seasons.get(str(m.season))
        if not cr_season_id:
            log.warning("no CR season id mapped for S%02d; skipping", m.season)
            continue
        log.info("=== S%02dE%02d -> CR season %s ep %d ===", m.season, m.episode, cr_season_id, cr_ep)
        try:
            src_path = dl.download_audio(cr_season_id, cr_ep, m.season)
        except Exception as e:
            log.error("download failed: %s", e)
            failed += 1
            continue
        try:
            jpn_idx = probe.jpn_audio_index(m.target_path)
            result = sync.detect(
                m.target_path, jpn_idx, str(src_path),
                trim_s=sync_cfg["trim_seconds"],
                bound_s=sync_cfg["bound_seconds"],
            )
            log.info("sync delay=%dms score=%.2f", result.delay_ms, result.score)
            if result.score < sync_cfg["min_score"]:
                log.warning("low confidence (%.2f); quarantining S%02dE%02d", result.score, m.season, m.episode)
                quarantined += 1
                continue
            if abs(result.delay_ms) > sync_cfg["max_abs_delay_ms"]:
                log.warning("delay %dms out of range; quarantining", result.delay_ms)
                quarantined += 1
                continue
            mux.inject(
                m.target_path, str(src_path), result.delay_ms,
                lang=cfg["target_language"]["audio"],
                track_name=cfg["target_language"]["audio_label"],
            )
            # cleanup staging file to free disk
            try:
                src_path.unlink()
            except Exception:
                pass
            ok += 1
        except Exception as e:
            log.exception("mux failed: %s", e)
            failed += 1

    if plex:
        try:
            plex.refresh_section()
        except Exception as e:
            log.warning("library refresh failed: %s", e)

    nt = cfg.get("ntfy") or {}
    if nt.get("url"):
        notify.ntfy(
            nt["url"], nt["topic"],
            f"{show['name']} dub injection: ok={ok} failed={failed} quarantined={quarantined}",
        )

    log.info("done: ok=%d failed=%d quarantined=%d", ok, failed, quarantined)
    return 0 if failed == 0 else 2


@click.group()
def cli():
    pass


@cli.command()
@click.option("--series", type=int, required=True, help="Sonarr seriesId")
@click.option("--config", "config_path", type=str, default=None)
@click.option("--dry-run", is_flag=True, help="List missing dubs without downloading")
def run(series: int, config_path: str | None, dry_run: bool):
    """Process a single Sonarr series end-to-end."""
    cfg = config.load(config_path)
    sys.exit(process_series(cfg, series, dry_run=dry_run))


@cli.command()
@click.option("--series", type=int, required=True)
@click.option("--config", "config_path", type=str, default=None)
def scan(series: int, config_path: str | None):
    """Just print episodes missing the target dub."""
    cfg = config.load(config_path)
    sys.exit(process_series(cfg, series, dry_run=True))


@cli.command()
def daemon():
    """Run as long-running service (FastAPI + scheduler + worker)."""
    from . import daemon as d
    d.run()


if __name__ == "__main__":
    cli()
