"""Mux a target-language audio track into an existing video container.

Performance ladder (fastest → slowest):

1. delay_ms >= 0
     `mkvmerge --sync 0:N` applies positive delay via container metadata.
     No re-encode, no audio rewrite. Roughly bound by disk/NFS rewrite of
     the target container (1-2 GB).

2. delay_ms < 0, AAC/AC3/EAC3/Opus/Vorbis/FLAC source
     ffmpeg `-ss <s> -c:a copy` does a frame-aligned codec-copy trim.
     No re-encode. Cuts seconds, not minutes.

3. delay_ms < 0, anything else
     ffmpeg re-encodes to AAC 192k after `atrim`. Slowest path; still
     limited to ~the audio duration on a modern CPU.

Local-disk staging (mux_workdir): when set, mkvmerge writes its output to
local disk, avoiding NFS small-write latency during the merge. The final
result is then copied to the target's NFS dir via a tempfile + os.replace
for atomic swap. Trades local-disk space for mux speed.
"""
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import probe
from .lang import lang_matches

log = logging.getLogger(__name__)


# ISO-639-2 → suffix shown in filename. Default suffix is "Dub-<lang>".
_LANG_SUFFIX = {
    "por": "Dublado-PT-BR",
    "spa": "Doblado-ES",
    "fra": "VF",
    "deu": "DE",
    "ita": "IT",
    "eng": "Dub-EN",
}

# Codecs that handle frame-aligned trim via `-c:a copy` cleanly. AAC frames are
# 1024 samples (~21ms @48kHz) so trim accuracy is well under sync error budget.
_COPYABLE_CODECS = {"aac", "ac3", "eac3", "opus", "vorbis", "flac", "mp3"}

# Above this size, local-disk mux staging is a net loss: mkvmerge becomes
# sequential-bandwidth bound (no small-write latency to amortize), and the
# copy-back round trip is pure overhead. 3 GB covers most WEBDL/HDTV with
# room to spare; Bluray Remux (6-15 GB) hits the direct path.
_LARGE_FILE_BYTES = 3 * 1024 ** 3

# Orphan tempfiles older than this get swept on next mux run.
_ORPHAN_MAX_AGE_S = 30 * 60  # 30 min

# Process-wide cap on concurrent mkvmerge runs. mkvmerge is NFS-I/O bound
# for big files; running 2+ in parallel splits bandwidth without speeding
# anything up — and adds load contention with the worker download phase.
# Workers can still run download + sync in parallel; only mux serializes here.
_MUX_SEM = threading.Semaphore(1)


def set_mux_concurrency(n: int) -> None:
    """Replace the mux semaphore with a new permit count. Call at daemon
    startup to override the default of 1 (e.g. on local-disk libraries
    where mkvmerge isn't NFS-bound)."""
    global _MUX_SEM
    _MUX_SEM = threading.Semaphore(max(1, int(n)))


def _filename_suffix(lang: str) -> str:
    return _LANG_SUFFIX.get(lang, f"Dub-{lang}")


def _run_or_raise(cmd: list[str], label: str) -> subprocess.CompletedProcess:
    """Run a subprocess, capture stderr, and raise RuntimeError with the actual
    error message instead of the generic CalledProcessError 'non-zero exit status N'."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        err_text = (r.stderr or r.stdout or "").strip()
        # Last meaningful line (skip blanks); cap to 320 chars to keep db rows sane
        last = next((ln for ln in reversed(err_text.splitlines()) if ln.strip()),
                    "(no output)")
        raise RuntimeError(f"{label} exit {r.returncode}: {last[:320]}")
    return r


def _run_mkvmerge(cmd: list[str]) -> subprocess.CompletedProcess:
    """mkvmerge has 3-level exit codes: 0=ok, 1=ok-with-warnings (file IS
    produced), 2=error (no file). Don't treat 1 as fatal — log warnings and
    continue."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    out_text = (r.stdout or "") + "\n" + (r.stderr or "")
    if r.returncode >= 2:
        err_lines = [ln for ln in out_text.splitlines() if ln.lower().startswith("error:")]
        if not err_lines:
            err_lines = [ln for ln in out_text.splitlines() if ln.strip()][-3:]
        msg = err_lines[-1] if err_lines else "(no output)"
        raise RuntimeError(f"mkvmerge exit {r.returncode}: {msg[:320]}")
    if r.returncode == 1:
        warnings = [ln for ln in out_text.splitlines() if ln.lower().startswith("warning:")]
        for w in warnings[:5]:
            log.warning("mkvmerge: %s", w[:240])
        if not warnings:
            log.warning("mkvmerge exit 1 with no Warning: lines (proceeding); output tail: %s",
                        out_text.splitlines()[-2:] if out_text else "")
    return r


def _audio_codec(path: str) -> str:
    """Return codec_name of the first audio stream (lower-case), or '' on failure."""
    try:
        for s in probe.streams(path):
            if s.get("codec_type") == "audio":
                return (s.get("codec_name") or "").lower()
    except Exception as e:
        log.warning("audio codec probe failed for %s: %s", path, e)
    return ""


def _trim_audio_copy(src: str, out: str, delay_ms: int) -> None:
    """Lossless frame-aligned trim via `-c:a copy`. Fast (seconds).

    Uses OUTPUT seek (`-ss` after `-i`) — frame-accurate for AAC. Input seek
    (`-ss` before `-i`) snaps to MKV cluster boundary and undershoots the
    requested trim by up to ~1s, breaking absolute-delay semantics."""
    trim_s = abs(delay_ms) / 1000
    _run_or_raise(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", src,
            "-ss", f"{trim_s:.3f}",
            "-vn", "-sn",
            "-map", "0:a:0",
            "-c:a", "copy",
            "-avoid_negative_ts", "make_zero",
            out,
        ],
        label="ffmpeg trim copy",
    )


def _trim_audio_reencode(src: str, out: str, delay_ms: int) -> None:
    """Fallback for codecs we can't safely copy. Re-encodes to AAC 192k."""
    trim_s = abs(delay_ms) / 1000
    af = f"atrim=start={trim_s:.3f},asetpts=PTS-STARTPTS"
    _run_or_raise(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", src,
            "-vn", "-sn",
            "-af", af,
            "-c:a", "aac", "-b:a", "192k",
            out,
        ],
        label="ffmpeg trim reencode",
    )


def _trim_audio(src: str, out: str, delay_ms: int) -> str:
    """Pick the fastest viable trim strategy. Returns mode label for logging."""
    if delay_ms >= 0:
        raise ValueError("_trim_audio only handles negative delays")
    codec = _audio_codec(src)
    if codec in _COPYABLE_CODECS:
        try:
            _trim_audio_copy(src, out, delay_ms)
            return f"copy({codec})"
        except RuntimeError as e:
            log.warning("codec-copy trim failed (%s); falling back to AAC re-encode", e)
            try:
                Path(out).unlink(missing_ok=True)
            except OSError:
                pass
    _trim_audio_reencode(src, out, delay_ms)
    return f"reencode-aac (src={codec or 'unknown'})"


def remux_with_new_delay(target_path: str, lang: str, new_delay_ms: int,
                          track_name: str = "Portuguese Brazil",
                          mux_workdir: str | None = None,
                          label_aliases: list[str] | None = None) -> str:
    """Re-apply sync delay to an already-muxed dub track without re-downloading.

    Extracts the existing dub track from the target via codec-copy, then runs
    it through inject() which handles both metadata-sync (positive delays) and
    frame-trim (negative delays). Avoids the CR re-download for manual-delay
    iteration.

    Why extract+inject instead of single-pass mkvmerge --sync:
      mkvmerge --sync writes container-level offset only. MKV cluster
      timestamps are unsigned — a negative absolute target (e.g. push por
      4s earlier than video) cannot be expressed and mkvmerge silently
      resets to the codec's intrinsic offset. inject() handles negative
      delays by trimming audio frames upfront with ffmpeg.

    Returns final muxed path. Raises if target has no track matching `lang`.
    """
    target_p = Path(target_path)
    target_dir = target_p.parent

    streams = [s for s in probe.streams(target_path, no_cache=True)
               if s.get("codec_type") == "audio"]
    # Com um dub original preservado no arquivo, o primeiro track no idioma
    # alvo pode ser o dele, nao o nosso. Recalibrar delay tem que pegar o track
    # que injetamos, entao casa pelo rotulo antes de cair no primeiro do idioma.
    rotulos = {track_name.strip().casefold()}
    rotulos |= {a.strip().casefold() for a in (label_aliases or []) if a.strip()}
    candidatos = [s for s in streams
                  if lang_matches((s.get("tags", {}).get("language") or "").lower(), lang)]
    nossos = [s for s in candidatos
              if (s.get("tags", {}).get("title") or "").strip().casefold() in rotulos]
    escolhido = (nossos or candidatos)
    if not escolhido:
        raise RuntimeError(f"remux: no audio track matching lang={lang} in target")
    dub_track = int(escolhido[0]["index"])

    work_parent = target_dir
    if mux_workdir:
        wp = Path(mux_workdir)
        try:
            wp.mkdir(parents=True, exist_ok=True)
            work_parent = wp
        except OSError:
            pass

    with tempfile.TemporaryDirectory(dir=work_parent, prefix="dubsmith-remux-") as td:
        extracted = os.path.join(td, "dub.mka")
        t_extract = time.time()
        _run_or_raise(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-i", target_path, "-map", f"0:{dub_track}", "-vn", "-sn",
             "-c", "copy", extracted],
            label="ffmpeg extract dub",
        )
        log.info("remux extract dub (tid=%d): %.1fs", dub_track, time.time() - t_extract)
        log.info("remux: target=%dms (delegating to inject for trim+mux)", new_delay_ms)
        return inject(target_path, extracted, new_delay_ms,
                      lang=lang, track_name=track_name, mux_workdir=mux_workdir,
                      label_aliases=label_aliases)


def _stat_same_fs(a: Path, b: Path) -> bool:
    """True when two paths live on the same device (no cross-FS rename needed)."""
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


def _sweep_orphan_tempfiles(target_dir: Path) -> int:
    """Remove stale `.dubsmith.*.mkv` tempfiles from prior interrupted copy-backs.
    Returns count removed."""
    n = 0
    cutoff = time.time() - _ORPHAN_MAX_AGE_S
    try:
        for p in target_dir.glob(".dubsmith.*.mkv"):
            try:
                if p.stat().st_mtime < cutoff:
                    sz = p.stat().st_size
                    p.unlink()
                    n += 1
                    log.info("swept orphan tempfile %s (%.1f MB)", p, sz / 1024 / 1024)
            except OSError:
                continue
    except OSError:
        pass
    return n


def inject(target: str, source: str, delay_ms: int,
           lang: str = "por", track_name: str = "Portuguese Brazil",
           mux_workdir: str | None = None,
           label_aliases: list[str] | None = None) -> str:
    """Mux source audio into target. Strips any pre-existing track of the same lang
    (resync case). Returns path to final file (may be renamed with a lang suffix).

    mux_workdir: when set, mkvmerge writes its output to that dir (typically local
    disk), then the result is copied to the target's NFS dir for atomic swap.
    Avoids NFS small-write latency during the merge. Falls back to target_dir
    if mux_workdir is unwritable.
    """
    t0 = time.time()
    target_p = Path(target)
    target_dir = target_p.parent
    stem = target_p.stem
    suffix = _filename_suffix(lang)
    if suffix in stem:
        new_name = stem + ".mkv"
    else:
        new_name = f"{stem} {suffix}.mkv"
    final = target_dir / new_name

    # Drop only OUR previous injection of the target lang (resync case). Um dub
    # que ja estava no arquivo (rip de BD, dub oficial que veio com o release)
    # nao e nosso e fica: se o track novo sair dessincronizado, o original ainda
    # esta la e continua sendo o default. So o track que carrega o nosso
    # track_name e considerado nosso, entao resync sucessivo nao empilha.
    #
    # `label_aliases` existe porque o track_name mudou ao longo do tempo: uma
    # injecao antiga rotulada "Portuguese" nao seria reconhecida por um deploy
    # que hoje escreve "Portuguese Brazil", e o remux empilharia as duas.
    # Lista os rotulos que ESTE deploy ja usou; nao ponha ai rotulo de dub que
    # veio no arquivo, senao ele vira candidato a ser descartado.
    keep_audio_indices: list[int] | None = None
    audios = [s for s in probe.streams(target) if s.get("codec_type") == "audio"]
    nossos_rotulos = {track_name.strip().casefold()}
    nossos_rotulos |= {a.strip().casefold() for a in (label_aliases or []) if a.strip()}

    def _nosso(s: dict) -> bool:
        if not lang_matches(s.get("tags", {}).get("language", ""), lang):
            return False
        titulo = (s.get("tags", {}).get("title") or "").strip().casefold()
        return titulo in nossos_rotulos

    if any(_nosso(s) for s in audios):
        keep_audio_indices = [int(s["index"]) for s in audios if not _nosso(s)]
        log.info("stripping previously injected %s audio (%r); keeping audio tracks %s",
                 lang, track_name, keep_audio_indices)
    else:
        preexistente = [
            int(s["index"]) for s in audios
            if lang_matches(s.get("tags", {}).get("language", ""), lang)
        ]
        if preexistente:
            log.info("keeping pre-existing %s audio %s as fallback; new track goes in "
                     "as non-default", lang, preexistente)

    # Sweep any orphan tempfiles from prior interrupted copy-backs in target dir.
    _sweep_orphan_tempfiles(target_dir)

    # Pick tempdir parent. Prefer mux_workdir (local disk) for speed; fall back
    # to target_dir if it doesn't exist or isn't writable. Skip local staging
    # for huge files where the copy-back round-trip outweighs the small-write
    # latency benefit on NFS.
    work_parent = target_dir
    work_local = False
    target_size = 0
    try:
        target_size = target_p.stat().st_size
    except OSError:
        pass
    if mux_workdir and target_size <= _LARGE_FILE_BYTES:
        wp = Path(mux_workdir)
        try:
            wp.mkdir(parents=True, exist_ok=True)
            work_parent = wp
            work_local = not _stat_same_fs(wp, target_dir)
        except OSError as e:
            log.warning("mux_workdir %s not usable (%s); falling back to target dir", mux_workdir, e)
    elif mux_workdir:
        log.info("target %.1f GB > %.1f GB — using target-FS staging (no copy-back)",
                 target_size / 1024 ** 3, _LARGE_FILE_BYTES / 1024 ** 3)

    with tempfile.TemporaryDirectory(dir=work_parent, prefix="dubsmith-mux-") as td:
        out_tmp = os.path.join(td, "out.mkv")
        cmd = ["mkvmerge", "-o", out_tmp]
        if keep_audio_indices:
            cmd += ["--audio-tracks", ",".join(str(i) for i in keep_audio_indices)]
        elif keep_audio_indices == []:
            # Lista vazia = todo audio do arquivo era injecao nossa. Sem esse
            # ramo o flag sumiria e o mkvmerge copiaria o track velho de volta,
            # duplicando o dub a cada resync.
            cmd += ["--no-audio"]
        cmd += [target]

        trim_mode = "n/a"
        if delay_ms >= 0:
            # Lossless path: container --sync metadata applied to next input.
            audio_in = source
            sync_arg = ["--sync", f"0:{delay_ms}"]
            trim_mode = "metadata-sync"
        else:
            t_trim = time.time()
            audio_in = os.path.join(td, "trimmed.mkv")
            trim_mode = _trim_audio(source, audio_in, delay_ms)
            log.info("trim: %s in %.1fs", trim_mode, time.time() - t_trim)
            sync_arg = []

        cmd += sync_arg + [
            "--no-video", "--no-attachments", "--no-chapters", "--no-buttons", "--no-track-tags",
            "--language", f"0:{lang}",
            "--track-name", f"0:{track_name}",
            "--default-track-flag", "0:0",
            audio_in,
        ]
        log.info("mkvmerge%s: %s", " (local-staged)" if work_local else "", " ".join(cmd))
        t_mux = time.time()
        # Serialize mkvmerge across workers to avoid NFS bandwidth fight.
        with _MUX_SEM:
            _run_mkvmerge(cmd)
        log.info("mkvmerge: %.1fs", time.time() - t_mux)

        orig_size = target_p.stat().st_size
        new_size = os.path.getsize(out_tmp)
        if new_size < orig_size * 0.9:
            raise RuntimeError(f"merged file suspiciously small: {new_size} < 90% of {orig_size}")

        # Land the file at `final` atomically. If we staged on local disk, copy
        # to a sibling tempfile on the target FS first so os.replace is atomic.
        if work_local:
            t_copy = time.time()
            nfs_tmp = target_dir / f".dubsmith.{os.getpid()}.{int(t_mux)}.mkv"
            try:
                shutil.copyfile(out_tmp, nfs_tmp)
                os.replace(nfs_tmp, final)
            except Exception:
                nfs_tmp.unlink(missing_ok=True)
                raise
            log.info("copy-back to target FS: %.1fs", time.time() - t_copy)
        else:
            os.replace(out_tmp, final)

    if str(final) != str(target_p):
        target_p.unlink(missing_ok=True)
    log.info("muxed: %s (delay=%dms, mode=%s, staging=%s, total=%.1fs)",
             final, delay_ms, trim_mode, "local" if work_local else "target",
             time.time() - t0)
    return str(final)
