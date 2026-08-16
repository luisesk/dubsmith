"""Pipeline worker: process one queued job end-to-end."""
import logging
import subprocess
import time
from pathlib import Path

from . import downloader, events, health, mux, notify, probe, staging, sync
from .lang import lang_matches, normalize
from .downloader import MdnxDownloader
from .queue import Job, Queue
from .shows import ShowsStore
from .sonarr import Sonarr

log = logging.getLogger(__name__)


# 30-day TTL on dub-cache entries. After calibration is dialed in for a show,
# the pristine copy is dead weight. Sweep on first job of a worker process.
_DUB_CACHE_TTL_S = 30 * 24 * 3600
_DUB_CACHE_SWEEP_DONE = False


def resolve_cr_season(entry, cr_ep: int, source: str = "crunchyroll",
                      on_normalize=None) -> str | None:
    """Resolve o id de season da CR para um episodio absoluto.

    `entry` (valor de cr_seasons[season]) aceita duas formas:

      '1': G675DKPNR                      # cour unico, comportamento antigo
      '1': [{id: G675DKPNR, first_ep: 1},
            {id: GS00363282JAJP, first_ep: 52}, ...]

    A forma em lista cobre serie que o Sonarr numera como uma temporada
    absoluta e a Crunchyroll parte em varias seasons (Black Clover, One Piece).
    Sem ela, todo episodio fora da primeira parte volta com "Episodes not
    selected!" do mdnx. Entrada em lista pode vir como id cru; nesse caso o
    first_ep e sondado uma vez e devolvido via `on_normalize` para gravar no
    shows.yml, de modo que a ida ao aniDL nao se repita.
    """
    if not entry:
        return None
    if isinstance(entry, str):
        return entry

    partes = []
    mudou = False
    for p in entry:
        if isinstance(p, dict):
            partes.append({"id": p["id"], "first_ep": int(p.get("first_ep") or 1)})
            continue
        first = downloader.probe_season_first_ep(str(p), source=source)
        partes.append({"id": str(p), "first_ep": int(first or 1)})
        mudou = True
    partes.sort(key=lambda p: p["first_ep"])
    if mudou and on_normalize:
        on_normalize(partes)

    escolhida = None
    for p in partes:
        if p["first_ep"] <= cr_ep:
            escolhida = p
    # Episodio abaixo do primeiro cour (numeracao estranha): fica com a parte 1.
    return (escolhida or partes[0])["id"] if partes else None


def _dub_cache_dir(cfg: dict) -> Path:
    """Pristine extracted dub audio kept here LAZILY — only on jobs that ran
    with manual_delay_ms set (signal that operator is iterating sync). Untouched
    eps never write here. Each entry is audio-only (.mka, ~30MB) extracted from
    the mdnx download to keep total cache size bounded."""
    return Path(cfg["paths"]["staging"]).parent / "dub-cache"


def _dub_cache_lookup(cfg: dict, series_id: int, season: int, episode: int,
                       lang: str) -> Path | None:
    base = _dub_cache_dir(cfg) / str(series_id) / f"S{season:02d}"
    if not base.exists():
        return None
    for p in base.glob(f"E{episode:02d}-{lang}.*"):
        return p
    return None


def _dub_cache_sweep(cfg: dict) -> int:
    """Delete cache files older than _DUB_CACHE_TTL_S. Returns count removed."""
    base = _dub_cache_dir(cfg)
    if not base.exists():
        return 0
    cutoff = time.time() - _DUB_CACHE_TTL_S
    n = 0
    for p in base.rglob("E*-*.*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                sz = p.stat().st_size
                p.unlink()
                n += 1
                log.info("dub-cache TTL evict: %s (%.1f MB)", p, sz / 1024 / 1024)
        except OSError:
            continue
    return n


def _dub_cache_write(cfg: dict, src_path: Path, series_id: int, season: int,
                      episode: int, lang: str) -> None:
    """Extract audio-only from the mdnx download to dub-cache.

    src_path is the full mdnx mkv (~1.4GB with video+audio). We keep only the
    first audio track as a stand-alone .mka (~30MB). 50× space savings."""
    cache_dir = _dub_cache_dir(cfg) / str(series_id) / f"S{season:02d}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"E{episode:02d}-{lang}.mka"
    tmp_path = cache_path.with_suffix(".mka.tmp")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src_path),
             "-map", "0:a:0", "-vn", "-sn", "-c", "copy", str(tmp_path)],
            check=True, capture_output=True, text=True,
        )
        tmp_path.replace(cache_path)
        log.info("cached audio-only dub for fast-remux: %s (%.1f MB)",
                 cache_path, cache_path.stat().st_size / 1024 / 1024)
    except subprocess.CalledProcessError as e:
        log.warning("dub-cache audio extract failed: %s", (e.stderr or "").strip()[:200])
        tmp_path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("dub-cache write failed: %s", e)
        tmp_path.unlink(missing_ok=True)


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

    # Priority order for picking a reference sync language. The earlier the
    # better — original-language audio (jpn for anime) maps best when the dub
    # was produced from it. CR uses 3-letter codes for --dubLang.
    _REF_LANG_PRIORITY = ["jpn", "eng", "fra", "deu", "spa", "ita"]

    def _pick_ref_lang(self, target_path: str, audio_lang: str,
                        cr_season_id: str, cfg: dict) -> tuple[str | None, int | None]:
        """Find a language present in BOTH the target file's audio tracks
        AND the CR season's available dub langs. Returns (cr_dub_lang_code,
        target_audio_stream_index). Either may be None if no overlap.
        """
        try:
            streams = [s for s in probe.streams(target_path)
                       if s.get("codec_type") == "audio"]
        except Exception as e:
            log.warning("ref-lang: probe failed: %s", e)
            return None, None
        # Target's existing langs (skip the one we're about to ADD)
        target_dub_norm = normalize(audio_lang)
        # Build {normalized_lang -> stream_index}
        target_langs: dict[str, int] = {}
        for s in streams:
            tag = s.get("tags", {}).get("language", "")
            n = normalize(tag)
            if n and n != target_dub_norm and n not in target_langs:
                target_langs[n] = int(s["index"])
        if not target_langs:
            return None, None
        # CR's available dubs for this season
        try:
            cr_langs = downloader.probe_season_dubs(cr_season_id, source=self.dl.source)
        except Exception:
            cr_langs = []
        cr_norms = {normalize(l) for l in cr_langs if l}
        # Walk priority list first
        for pref in self._REF_LANG_PRIORITY:
            if pref in target_langs and pref in cr_norms:
                return pref, target_langs[pref]
        # Fallback: any overlap (deterministic by dict iteration order)
        for n, idx in target_langs.items():
            if n in cr_norms:
                return n, idx
        return None, None

    def process(self, job: Job) -> str | None:
        """Roda o job. Devolve "deferred" quando o teto de taxa adiou o
        download (o daemon dorme nesse caso), None em qualquer outro desfecho."""
        cfg = self.cfg
        bus = events.get_bus()

        # One-shot TTL sweep on first job after worker startup. Cheap (just
        # scans dub-cache mtimes) and catches stale entries from prior runs.
        global _DUB_CACHE_SWEEP_DONE
        if not _DUB_CACHE_SWEEP_DONE:
            _DUB_CACHE_SWEEP_DONE = True
            try:
                evicted = _dub_cache_sweep(cfg)
                if evicted:
                    log.info("dub-cache TTL sweep: evicted %d stale entries", evicted)
            except Exception as e:
                log.warning("dub-cache sweep failed: %s", e)

        # Fast path: manual delay AND cached pristine audio exists → re-mux
        # with absolute delay, no CR re-download. ~25s vs minutes.
        # No cache → fall through to full pipeline (which downloads + caches
        # because manual_delay_ms is set, signaling operator iteration).
        log.info("fast-path check: job %d manual_delay=%s target_exists=%s",
                 job.id, job.manual_delay_ms,
                 Path(job.target_path).exists() if job.target_path else False)
        if job.manual_delay_ms is not None and Path(job.target_path).exists():
            try:
                show_quick = self.shows.get(job.series_id) or {}
                audio_lang = show_quick.get("target_audio") or cfg["target_language"]["audio"]
                audio_label = show_quick.get("target_audio_label") or cfg["target_language"]["audio_label"]
                cached = _dub_cache_lookup(cfg, job.series_id, job.season,
                                            job.episode, audio_lang)
                if cached:
                    log.info("fast-path: using cached pristine audio %s", cached)
                    self.queue.set_state(job.id, "muxing",
                                         sync_delay_ms=job.manual_delay_ms,
                                         sync_score=999.0)
                    mux_workdir = (cfg.get("paths") or {}).get(
                        "mux_workdir") or str(Path(cfg["paths"]["staging"]).parent / "mux")
                    final_path = mux.inject(
                        job.target_path, str(cached), job.manual_delay_ms,
                        lang=audio_lang, track_name=audio_label,
                        mux_workdir=mux_workdir,
                        label_aliases=cfg["target_language"].get("audio_label_aliases"),
                    )
                    # Refresh mtime so TTL clock resets on each iteration
                    try:
                        cached.touch()
                    except OSError:
                        pass
                    self.queue.set_state(job.id, "done", target_path=final_path)
                    bus.publish("job", {"id": job.id, "series_id": job.series_id,
                                          "season": job.season, "episode": job.episode,
                                          "state": "done", "sync_delay_ms": job.manual_delay_ms})
                    log.info("done (fast remux): job %d delay=%dms",
                             job.id, job.manual_delay_ms)
                    return
                else:
                    log.info("fast-path: no cache — full re-download will populate cache "
                             "for future iterations (lazy cache policy)")
            except Exception as e:
                log.warning("fast-remux failed (%s); falling back to full re-download", e)


        def emit(state: str, **extra) -> None:
            bus.publish("job", {"id": job.id, "series_id": job.series_id,
                                 "season": job.season, "episode": job.episode,
                                 "state": state, **extra})

        emit("started")
        show = self.shows.get(job.series_id) or cfg.get("shows", {}).get(job.series_id) or cfg.get("shows", {}).get(str(job.series_id))
        if not show:
            self.queue.set_state(job.id, "failed", last_error=f"no show config for series {job.series_id}")
            return

        cr_seasons = show.get("cr_seasons", {})
        season_offset = show.get("season_offset", {})
        season_entry = cr_seasons.get(str(job.season))
        cr_ep = job.episode + season_offset.get(str(job.season), 0)
        multi_cour = isinstance(season_entry, list)

        def _persist_partes(partes):
            novo = dict(cr_seasons)
            novo[str(job.season)] = partes
            self.shows.upsert(job.series_id, cr_seasons=novo)

        cr_season_id = resolve_cr_season(
            season_entry, cr_ep,
            source=show.get("source", "crunchyroll"),
            on_normalize=_persist_partes,
        )
        if not cr_season_id:
            self.queue.set_state(job.id, "failed", last_error=f"no cr_seasons mapping for S{job.season}")
            return

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
        except downloader.TetoDeTaxaExcedido as e:
            # Nao e falha do episodio, e a nossa protecao de conta dizendo
            # "agora nao". Volta para pending devolvendo a tentativa que o
            # claim_next consumiu, senao um retry em massa esgotaria
            # max_attempts em poucas horas sem nunca ter tentado de verdade.
            log.info("[job %s] adiado pelo teto de taxa: %s", job.id, e)
            self.queue.set_state(job.id, "pending",
                                 attempts=max(0, (job.attempts or 1) - 1),
                                 last_error=f"adiado: {e}")
            _clean()
            # "deferred" avisa o daemon para dormir ate a proxima vaga. Devolver
            # None faria ele reclamar este mesmo job no ciclo seguinte.
            return "deferred"
        except Exception as e:
            err = str(e)
            # Auto-recovery: when mdnx returns "Episodes not selected!" for a season
            # without a season_offset configured, probe the season's first absolute
            # episode number, save the inferred offset to shows.yml, and retry.
            # Crunchyroll continuation cours start at non-1 absolute numbers
            # (S02 starts at 13, etc.) and need this offset.
            # Mapeamento multi-cour ja usa numeracao absoluta: inferir offset
            # ali deslocaria o episodio de novo e quebraria as partes que funcionam.
            if "Episodes not selected" in err and not multi_cour:
                season_offset_cur = season_offset.get(str(job.season), 0)
                if season_offset_cur == 0:
                    first = downloader.probe_season_first_ep(cr_season_id, source=self.dl.source)
                    if first and first > 1:
                        offset = first - 1
                        log.info("auto-detected season_offset for sid=%d S%02d: %d (CR season starts at ep %d)",
                                 job.series_id, job.season, offset, first)
                        new_offsets = dict(season_offset)
                        new_offsets[str(job.season)] = offset
                        self.shows.upsert(job.series_id, season_offset=new_offsets)
                        # Reset to pending — next worker pickup will use the new offset.
                        self.queue.set_state(job.id, "pending",
                                             last_error=f"auto-detected season_offset={offset}, retrying")
                        _clean()
                        return
                health.report_episodes_not_selected()
            self.queue.set_state(job.id, "failed", last_error=f"download: {err}")
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
            sync_cfg = cfg["sync"]
            try:
                jpn_idx = probe.jpn_audio_index(job.target_path)
                result = sync.detect(
                    job.target_path, jpn_idx, str(src_path),
                    skip_s=sync_cfg["trim_seconds"],
                    bound_s=sync_cfg["bound_seconds"],
                )
                # Per-show manual offset override — added on top of detection
                show_offset = int(show.get("audio_offset_ms") or 0)
                if show_offset:
                    log.info("per-show offset: %dms (detected=%dms → adjusted=%dms)",
                             show_offset, result.delay_ms, result.delay_ms + show_offset)
                    result.delay_ms = result.delay_ms + show_offset
            except Exception as e:
                self.queue.set_state(job.id, "failed", last_error=f"sync: {e}")
                _clean()
                return

            log.info("sync delay=%dms score=%.2f", result.delay_ms, result.score)

            # Concordancia entre janelas, nao prominencia de pico, e o que diz se
            # o valor esta certo. O score mede se existe um pico afiado; num caso
            # medido ele deu 20,9 para um lag 224ms errado enquanto as janelas
            # discordavam em 3761ms. Espalhamento grande significa ou pico errado
            # numa janela ou versoes com cortes diferentes, e nos dois casos nao
            # existe um delay unico que sirva para o episodio inteiro.
            spread_max = int(cfg["sync"].get("max_window_spread_ms", 50))
            janelas = result.windows or []
            spread = (max(janelas) - min(janelas)) if len(janelas) > 1 else 0
            if spread > spread_max:
                self.queue.set_state(
                    job.id, "quarantined",
                    sync_delay_ms=result.delay_ms, sync_score=result.score,
                    last_error=(f"janelas discordam em {spread}ms (teto {spread_max}ms): "
                                f"{janelas} — nenhum delay unico serve"),
                )
                _clean()
                return

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
            final_path = mux.inject(
                job.target_path, str(src_path), result_delay,
                lang=audio_lang, track_name=audio_label,
                mux_workdir=mux_workdir,
                label_aliases=cfg["target_language"].get("audio_label_aliases"),
            )
        except Exception as e:
            self.queue.set_state(job.id, "failed", last_error=f"mux: {e}")
            _clean()
            return

        # Lazy dub-cache: only populate when operator is iterating sync
        # (manual_delay_ms set on the job). 99%+ of eps where cross-corr is
        # fine never write here, keeping total cache size bounded. Audio-only
        # extraction (~30MB vs ~1.4GB full mdnx mkv) — see _dub_cache_write.
        if job.manual_delay_ms is not None:
            _dub_cache_write(cfg, src_path, job.series_id, job.season,
                             job.episode, audio_lang)

        # Success: nuke the per-episode staging dir + prune empty parents.
        # Update target_path to the renamed final file so retries (manual delay,
        # re-detect) operate on the file that actually exists post-mux.
        _clean()
        self.queue.set_state(job.id, "done", target_path=final_path)
        emit("done", sync_delay_ms=result_delay)
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
        notify.push(
            cfg.get("ntfy") or {},
            f"{show.get('name','?')} S{job.season:02d}E{job.episode:02d} dub injected (delay {result_delay}ms)",
        )
