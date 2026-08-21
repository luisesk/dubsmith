"""Pipeline worker: process one queued job end-to-end."""
import logging
import math
import os
import shutil
import statistics
import subprocess
import time
from pathlib import Path

from . import downloader, events, health, mux, notify, probe, staging, sync, verify
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

# Serve so para responder "esta pasta e mesmo de uma serie da library?" antes
# de decidir baixar um episodio inteiro.
_EXT_VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv"}

# Altura a partir da qual um arquivo da library nunca pode ser substituido pelo
# download da CR, aconteca o que acontecer com o sync. A CR entrega no maximo
# 1080p, entao a troca de um 2160p seria perda garantida e irreversivel.
_ALTURA_INTOCAVEL = 1440


def _consenso_janelas(janelas: list[int], cfg: dict) -> tuple[bool, str]:
    """Diz se as janelas de deteccao concordam o bastante para valer um delay.

    Espelha `verify._julgar`: mediana primeiro, depois quantas janelas caem
    perto dela. Amplitude crua reprova por causa de uma janela solitaria, e a
    mediana que o `sync` devolve nem chega a olhar para essa janela.

    Devolve (True, "") quando passa, ou (False, motivo) quando nao.
    """
    if len(janelas) < 2:
        return True, ""
    conf = cfg.get("sync") or {}
    # consenso_ms tem que ser MENOR que spread_max, senao o gate se auto-sabota:
    # uma janela a exatamente spread_max da mediana entra no grupo dos
    # concordantes e estoura o spread desse mesmo grupo. Com os dois em 50 o
    # Frieren S02E07 reprovou com janelas [7, 8, -43, 8], spread 51 por 1ms.
    # Mesma proporcao que o verify._julgar ja usava (25 contra 60).
    consenso_ms = int(conf.get("consenso_ms", 25))
    spread_max = int(conf.get("max_window_spread_ms", 50))
    frac = float(conf.get("min_consenso_frac", 0.6))

    mediana = int(statistics.median(janelas))
    concordam = [l for l in janelas if abs(l - mediana) <= consenso_ms]
    minimo = max(2, math.ceil(len(janelas) * frac))
    spread = max(concordam) - min(concordam) if concordam else 0

    if len(concordam) < minimo:
        return False, (f"janelas discordam: so {len(concordam)}/{len(janelas)} "
                       f"dentro de {consenso_ms}ms da mediana {mediana}ms "
                       f"(minimo {minimo}): {janelas}, nenhum delay unico serve")
    if spread > spread_max:
        return False, (f"janelas concordantes ainda espalhadas: {spread}ms "
                       f"(teto {spread_max}ms) entre {concordam}")
    return True, ""


def _altura_video(caminho: str) -> int | None:
    """Altura em pixels do primeiro stream de video, ou None se nao der para ler."""
    try:
        for s in probe.streams(caminho):
            if s.get("codec_type") == "video" and s.get("height"):
                return int(s["height"])
    except Exception as e:
        log.warning("nao deu para medir a altura de %s: %s", caminho, e)
    return None


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
        # -f matroska e obrigatorio: o arquivo temporario termina em .tmp e o
        # ffmpeg nao consegue inferir o container pela extensao, entao sem isso
        # toda escrita no cache falha e cada iteracao do operador re-baixa.
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src_path),
             "-map", "0:a:0", "-vn", "-sn", "-c", "copy",
             "-f", "matroska", str(tmp_path)],
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

    def _verify_cb(self, job, applied_ms, audio_lang, audio_label, cfg, box):
        """Callback para mux.inject(verify_cb=...). Levanta VerificationFailed
        para o merge nunca aterrissar quando o artefato nao se certifica."""
        vcfg = cfg.get("verify") or {}

        def _cb(tmp_path: str) -> None:
            res = verify.verify_muxed(
                tmp_path,
                dub_lang=audio_lang,
                track_name=audio_label,
                label_aliases=cfg["target_language"].get("audio_label_aliases"),
                applied_delay_ms=applied_ms,
                cfg=vcfg,
            )
            box["result"] = res
            log.info("verify job %d aplicado=%dms: %s", job.id, applied_ms, res.summary())
            if not res.ok:
                raise verify.VerificationFailed(res)

        return _cb

    def _mux_verified(self, job, target_path, source_path, delay_ms,
                      audio_lang, audio_label, mux_workdir, cfg):
        """mux.inject mais verificacao, com no maximo UMA correcao automatica
        reinjetada do MESMO audio original.

        Devolve (caminho_final, VerifyResult, delay_aplicado). Levanta
        VerificationFailed quando o artefato nao se certifica, e nesse caso o
        arquivo da library segue intacto, porque a verificacao roda antes da
        substituicao atomica dentro do mux.inject.

        A correcao reexecuta mux.inject a partir do audio original com delay
        ABSOLUTO, que e a mesma chamada do caminho normal. Nunca via
        mux.remux_with_new_delay, que e relativo para faixa ja cortada.
        """
        vcfg = cfg.get("verify") or {}
        aliases = cfg["target_language"].get("audio_label_aliases")
        if not vcfg.get("enabled", True):
            final = mux.inject(target_path, source_path, delay_ms,
                               lang=audio_lang, track_name=audio_label,
                               mux_workdir=mux_workdir, label_aliases=aliases)
            return final, None, delay_ms

        max_passes = 2 if vcfg.get("auto_correct", True) else 1
        teto = int(vcfg.get("auto_correct_max_ms",
                            verify.DEFAULTS["auto_correct_max_ms"]))
        aplicado = delay_ms
        ultimo = None
        for tentativa in range(max_passes):
            box: dict = {}
            try:
                final = mux.inject(
                    target_path, source_path, aplicado,
                    lang=audio_lang, track_name=audio_label,
                    mux_workdir=mux_workdir, label_aliases=aliases,
                    verify_cb=self._verify_cb(job, aplicado, audio_lang,
                                              audio_label, cfg, box),
                )
                return final, box.get("result"), aplicado
            except verify.VerificationFailed as e:
                ultimo = e.result
                pode = (tentativa + 1 < max_passes
                        and ultimo.confident
                        and ultimo.residual_ms is not None
                        and abs(ultimo.residual_ms) <= teto)
                if not pode:
                    raise
                corrigido = aplicado + ultimo.residual_ms
                log.warning("job %d reprovou (%s); corrigindo %dms -> %dms "
                            "a partir do audio original",
                            job.id, ultimo.reason, aplicado, corrigido)
                self.queue.update_progress(job.id, phase="corrigindo sync")
                aplicado = corrigido
        raise verify.VerificationFailed(ultimo)

    def _quarantine_unverified(self, job, res, aplicado, cfg,
                               src_path=None, audio_lang=None):
        """Registra o residuo medido e a correcao ja calculada, e guarda o audio
        original no cache para a retentativa do operador cair no caminho rapido
        em vez de gastar um download da Crunchyroll."""
        if res is None:
            msg = "verificacao pos-mux falhou (sem resultado)"
        elif res.residual_ms is None:
            msg = f"verificacao pos-mux: {res.reason}; ref[{res.ref_desc}]"
        else:
            msg = (f"verificacao pos-mux: {res.reason}; "
                   f"residuo={res.residual_ms:+d}ms spread={res.spread_ms}ms "
                   f"uteis={res.n_usable}/{res.n_windows} ref[{res.ref_desc}]; "
                   f"delay manual sugerido {res.suggested_delay_ms:+d}ms")
        if src_path is not None and audio_lang is not None:
            try:
                _dub_cache_write(cfg, src_path, job.series_id, job.season,
                                 job.episode, audio_lang)
            except Exception as e:
                log.warning("dub-cache na quarentena falhou: %s", e)
        self.queue.set_state(job.id, "quarantined",
                             sync_delay_ms=aplicado, last_error=msg)
        log.warning("job %d em quarentena pela verificacao: %s", job.id, msg)

    def _pode_preencher(self, job, cfg) -> str | None:
        """Devolve None quando vale baixar o episodio inteiro, ou o motivo de nao.

        O arquivo alvo ausente tem duas leituras muito diferentes: buraco de
        verdade na library, que e o caso interessante, ou volume montado errado,
        quando `paths.library_in_container` e `paths_extra.sonarr_prefix`
        divergem e TODO caminho some de uma vez. Confundir os dois seria caro:
        no segundo caso o dubsmith baixaria o catalogo inteiro achando que a
        library esta vazia.

        O que separa os dois e a pasta da serie. Se ela existe e ja tem video
        dentro, a montagem esta certa e o que falta e um episodio. Se ela nao
        existe, nao da para saber, e ai o comportamento antigo (falhar com a
        dica de path) e o certo.
        """
        conf = (cfg.get("fill_missing") or {})
        if not conf.get("enabled", True):
            return (f"target file not found: {job.target_path} — preenchimento "
                    f"desligado (fill_missing.enabled)")

        destino = Path(job.target_path)
        pasta = destino.parent
        if not pasta.is_dir():
            return (f"target file not found: {job.target_path} — a pasta da serie "
                    f"tambem nao existe, o que aponta para montagem errada; "
                    f"confira paths.library_in_container vs "
                    f"paths_extra.sonarr_prefix no config.yml")

        vizinhos = [p for p in pasta.rglob("*")
                    if p.suffix.lower() in _EXT_VIDEO and p.is_file()]
        if not vizinhos:
            return (f"target file not found: {job.target_path} — a pasta existe "
                    f"mas esta sem nenhum video, entao nao da para afirmar que "
                    f"a library esta montada certo; conferir antes de baixar")
        return None

    def _substituir_por_download(self, job, show, src_path, cfg, emit, motivo) -> bool:
        """Ultimo recurso quando nenhum delay serve: usar o episodio da CR inteiro.

        Chega aqui um job cujo dub existe (o download acabou de trazer) mas cujo
        sync nao fecha: as janelas discordam em segundos, ou a verificacao
        reprovou depois da correcao. Injetar assim mesmo entregaria um arquivo
        torto, e mandar para fila humana nao resolve nada, porque ninguem vai
        abrir episodio por episodio.

        Trocar o arquivo resolve pela raiz. O que a Crunchyroll entrega ja vem
        com video e dub no mesmo relogio, entao a pergunta do sync deixa de
        existir. O preco e a qualidade da imagem virar a da CR, que pode ser
        menor que a do arquivo que estava ali.

        Por isso a troca so vale quando a imagem nao piora. A CR entrega no
        maximo 1080p e em serie antiga entrega 480p: medido em 2026-08-17, 34
        das 122 trocas ja feitas aterrissaram em 640x480 ou 656x480. Um 2160p
        nunca e trocado, e nenhuma altura pode cair. O arquivo antigo some no
        `os.replace`, entao a checagem tem que vir antes.

        Devolve True quando a troca aconteceu.
        """
        conf = (cfg.get("fill_missing") or {})
        if not conf.get("replace_on_unsyncable", True):
            return False

        alvo = Path(job.target_path)
        if alvo.exists():
            altura_alvo = _altura_video(str(alvo))
            altura_cr = _altura_video(str(src_path))
            if altura_alvo is None or altura_cr is None:
                log.warning("job %d: nao consegui comparar as alturas "
                            "(library=%s CR=%s); nao vou trocar",
                            job.id, altura_alvo, altura_cr)
                return False
            if altura_alvo >= _ALTURA_INTOCAVEL:
                log.warning("job %d: arquivo da library tem %dp, acima do teto "
                            "intocavel de %dp; troca bloqueada",
                            job.id, altura_alvo, _ALTURA_INTOCAVEL)
                return False
            if altura_cr < altura_alvo:
                log.warning("job %d: a CR entrega %dp contra %dp que ja esta na "
                            "library; troca bloqueada para nao perder resolucao",
                            job.id, altura_cr, altura_alvo)
                return False

        try:
            self._aterrissar_episodio(job, show, src_path, cfg, emit,
                                      substituir=True, motivo=motivo)
            return True
        except Exception as e:
            log.warning("job %d: troca pelo episodio da CR falhou (%s); "
                        "seguindo para quarentena", job.id, e)
            return False

    def _aterrissar_episodio(self, job, show, src_path, cfg, emit,
                             substituir: bool = False, motivo: str = "") -> None:
        """Move o episodio baixado para o lugar dele na library.

        Chamado quando o arquivo alvo nao existia. O mkv que o mdnx entrega ja
        vem completo (video, audio dublado e legendas), porque o download do
        dubsmith nunca passou `--novids`: ate hoje o pipeline extraia so o audio
        e jogava o resto fora. Aqui o arquivo inteiro fica.

        Nao ha sync a fazer. O audio dublado e o que a propria Crunchyroll
        entregou junto do video, entao os dois ja nascem no mesmo relogio, e a
        verificacao pos-mux, que compara dub contra faixa japonesa da library,
        nao tem o que comparar.
        """
        origem = Path(src_path)
        dub = show.get("cr_dub_lang") or cfg["target_language"]["cr_dub_lang"]

        fluxos = probe.streams(str(origem))
        if not any(s.get("codec_type") == "video" for s in fluxos):
            raise RuntimeError(f"o download nao trouxe video: {origem.name}")
        if not probe.has_audio_lang(str(origem), dub):
            raise RuntimeError(f"o download nao trouxe audio {dub}: {origem.name}")

        # O nome vem do que o Sonarr registrou, mas com a extensao do que
        # baixamos: o caminho antigo pode dizer .avi enquanto o mdnx entrega
        # mkv. O Sonarr renomeia depois, no rescan.
        destino = Path(job.target_path).with_suffix(origem.suffix)
        antigo = Path(job.target_path)
        if destino.exists() and not substituir:
            raise RuntimeError(f"destino ja existe, nao vou sobrescrever: {destino}")

        # Copia para um temporario ao lado do destino e so entao renomeia. A
        # library e outro volume, entao nao existe rename atomico vindo do
        # staging; o os.replace no fim garante que ninguem veja meio arquivo.
        parcial = destino.with_name(destino.name + ".dubsmith-parcial")
        self.queue.update_progress(job.id, progress=0.9, phase="landing in library")
        try:
            shutil.copyfile(origem, parcial)
            os.replace(parcial, destino)
        finally:
            if parcial.exists():
                parcial.unlink(missing_ok=True)

        # Trocar .avi por .mkv deixaria os dois lado a lado, e o Sonarr passaria
        # a ver o episodio duplicado. So depois do os.replace, que e o ponto em
        # que o novo ja esta inteiro no lugar.
        if substituir and antigo.exists() and antigo != destino:
            try:
                antigo.unlink()
                log.info("job %d: removido o arquivo antigo %s", job.id, antigo.name)
            except OSError as e:
                log.warning("job %d: nao deu para remover %s: %s", job.id, antigo.name, e)

        tamanho = destino.stat().st_size
        if substituir:
            log.warning("job %d: arquivo TROCADO pelo episodio da CR, %s (%.0f MB); "
                        "motivo: %s", job.id, destino.name, tamanho / 1e6, motivo)
        else:
            log.info("job %d: buraco preenchido, %s (%.0f MB)",
                     job.id, destino.name, tamanho / 1e6)

        # sync_delay_ms=0 descreve a verdade: nada foi deslocado. Deixar nulo
        # faria a proxima analise achar que o job nunca mediu nada.
        self.queue.set_state(job.id, "done", target_path=str(destino),
                             sync_delay_ms=0, sync_score=999.0)
        emit("done", sync_delay_ms=0)

        settings_data = self.settings.load() if self.settings else {}
        if (settings_data.get("sonarr", {}) or {}).get("rescan_after_mux", True):
            try:
                self.sonarr.rescan_series(job.series_id)
            except Exception as e:
                log.warning("sonarr rescan falhou: %s", e)
        notify.push(
            cfg.get("ntfy") or {},
            f"{show.get('name','?')} S{job.season:02d}E{job.episode:02d} "
            f"baixado inteiro (a library nao tinha o arquivo)",
        )

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
                    try:
                        final_path, _vres, aplicado = self._mux_verified(
                            job, job.target_path, str(cached),
                            job.manual_delay_ms, audio_lang, audio_label,
                            mux_workdir, cfg)
                    except verify.VerificationFailed as ve:
                        # Nao cair no except de baixo, que reparte para
                        # re-download: o audio original ja esta em cache e
                        # baixar de novo queimaria vaga do teto a toa.
                        self._quarantine_unverified(job, ve.result,
                                                    job.manual_delay_ms, cfg)
                        try:
                            cached.touch()
                        except OSError:
                            pass
                        return
                    # Refresh mtime so TTL clock resets on each iteration
                    try:
                        cached.touch()
                    except OSError:
                        pass
                    self.queue.set_state(job.id, "done", target_path=final_path,
                                         sync_delay_ms=aplicado)
                    bus.publish("job", {"id": job.id, "series_id": job.series_id,
                                          "season": job.season, "episode": job.episode,
                                          "state": "done", "sync_delay_ms": aplicado})
                    log.info("done (fast remux): job %d delay=%dms",
                             job.id, aplicado)
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

        # Arquivo alvo ausente nao e mais fim de linha: quando da para provar que
        # a library esta montada certo, o episodio inteiro e baixado e ocupa o
        # lugar vazio. `_pode_preencher` e quem separa buraco de montagem errada.
        preencher = not Path(job.target_path).exists()
        if preencher:
            motivo = self._pode_preencher(job, cfg)
            if motivo:
                self.queue.set_state(job.id, "failed", last_error=motivo)
                return
            log.info("job %d: library sem o arquivo; baixando o episodio inteiro "
                     "para %s", job.id, job.target_path)

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
        except downloader.DublagemIndisponivel as e:
            # Terminal por natureza. Encerra como "done" em vez de "failed"
            # justamente para a varredura periodica nao re-enfileirar o
            # episodio a cada ciclo e o retry nao gastar mais duas vagas.
            log.info("[job %s] sem dublagem no catalogo: %s", job.id, e)
            self.queue.set_state(job.id, "done",
                                 last_error=f"indisponivel: {e}")
            _clean()
            return
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

        # Buraco na library: o episodio baixado vira o arquivo, e acabou. Nada
        # de deteccao nem de mux, que so fazem sentido quando ja existe um
        # arquivo com faixa japonesa para servir de relogio.
        if preencher:
            try:
                self._aterrissar_episodio(job, show, src_path, cfg, emit)
            except Exception as e:
                self.queue.set_state(job.id, "failed", last_error=f"preenchimento: {e}")
            _clean()
            return

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
            # discordavam em 3761ms.
            #
            # O que se pergunta aqui e se a MAIORIA das janelas concorda, nao se
            # a pior delas concorda, que e a mesma leitura que verify._julgar ja
            # fazia do outro lado do mux. Amplitude crua (max - min) reprova um
            # arquivo bom quando uma unica janela cai em musica ou silencio:
            # [-46, 3009, -46, -45] tem tres janelas dentro de 1ms e mesmo assim
            # media 3055ms de amplitude. Medido em 2026-08-17, 50 dos 121
            # arquivos que o portao antigo mandou trocar tinham consenso claro.
            janelas = result.windows or []
            ok_consenso, razao = _consenso_janelas(janelas, cfg)
            if not ok_consenso:
                # O download ja esta na mao e traz video junto. Se nenhum delay
                # serve, o arquivo da CR inteiro serve, porque nele o dub e o
                # video ja saem sincronizados da origem.
                if self._substituir_por_download(job, show, src_path, cfg, emit, razao):
                    _clean()
                    return
                self.queue.set_state(
                    job.id, "quarantined",
                    sync_delay_ms=result.delay_ms, sync_score=result.score,
                    last_error=razao,
                )
                _clean()
                return

            # min_score deixou de barrar. O score mede afiacao de pico, nao
            # acerto, e errou nos dois sentidos: aprovou um job 226ms fora com
            # 20,9 e reprovou 150 episodios que a verificacao pos-mux teria
            # medido, corrigido e liberado. Barrar por proxy segurava trabalho
            # que o teste real aprova. Quem decide agora e a verificacao do
            # artefato pronto; isto aqui virou aviso.
            if result.score < cfg["sync"]["min_score"]:
                log.warning("job %d score baixo (%.2f < %s); seguindo assim mesmo, "
                            "a verificacao pos-mux decide",
                            job.id, result.score, cfg["sync"]["min_score"])
            if abs(result.delay_ms) > cfg["sync"]["max_abs_delay_ms"]:
                razao = (f"delay {result.delay_ms}ms fora da faixa "
                         f"(teto {cfg['sync']['max_abs_delay_ms']}ms)")
                if self._substituir_por_download(job, show, src_path, cfg, emit, razao):
                    _clean()
                    return
                self.queue.set_state(
                    job.id, "quarantined",
                    sync_delay_ms=result.delay_ms, sync_score=result.score,
                    last_error=razao + " — set manual delay if needed",
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
            final_path, _vres, result_delay = self._mux_verified(
                job, job.target_path, str(src_path), result_delay,
                audio_lang, audio_label, mux_workdir, cfg)
        except verify.VerificationFailed as ve:
            # Nada aterrissou: o mux.inject levantou antes da substituicao
            # atomica, entao o arquivo da library segue intacto. A correcao ja
            # teve a chance dela dentro do _mux_verified, entao chegar aqui
            # significa que o dub nao encaixa nesse arquivo por delay nenhum.
            if self._substituir_por_download(job, show, src_path, cfg, emit,
                                             f"verificacao pos-mux: {ve.result.reason}"
                                             if ve.result else "verificacao pos-mux"):
                _clean()
                return
            # Guarda o audio original para a retentativa com delay manual cair
            # no caminho rapido (~25s) em vez de gastar um download.
            self._quarantine_unverified(job, ve.result, result_delay, cfg,
                                        src_path=src_path, audio_lang=audio_lang)
            _clean()
            return
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
        # sync_delay_ms tem que registrar o delay REALMENTE aplicado, que muda
        # quando a verificacao corrige, senao o banco descreve um arquivo que
        # nao existe e a proxima analise parte de numero errado.
        self.queue.set_state(job.id, "done", target_path=final_path,
                             sync_delay_ms=result_delay)
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
