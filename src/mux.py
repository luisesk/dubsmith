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
"""
import logging
import os
import subprocess
import tempfile
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


def _filename_suffix(lang: str) -> str:
    return _LANG_SUFFIX.get(lang, f"Dub-{lang}")


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
    """Lossless frame-aligned trim via `-c:a copy`. Fast (seconds)."""
    trim_s = abs(delay_ms) / 1000
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{trim_s:.3f}",
            "-i", src,
            "-vn", "-sn",
            "-map", "0:a:0",
            "-c:a", "copy",
            out,
        ],
        check=True,
    )


def _trim_audio_reencode(src: str, out: str, delay_ms: int) -> None:
    """Fallback for codecs we can't safely copy. Re-encodes to AAC 192k."""
    trim_s = abs(delay_ms) / 1000
    af = f"atrim=start={trim_s:.3f},asetpts=PTS-STARTPTS"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", src,
            "-vn", "-sn",
            "-af", af,
            "-c:a", "aac", "-b:a", "192k",
            out,
        ],
        check=True,
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
        except subprocess.CalledProcessError as e:
            log.warning("codec-copy trim failed (%s); falling back to AAC re-encode", e)
            try:
                Path(out).unlink(missing_ok=True)
            except OSError:
                pass
    _trim_audio_reencode(src, out, delay_ms)
    return f"reencode-aac (src={codec or 'unknown'})"


def inject(target: str, source: str, delay_ms: int,
           lang: str = "por", track_name: str = "Portuguese Brazil") -> str:
    """Mux source audio into target. Strips any pre-existing track of the same lang
    (resync case). Returns path to final file (may be renamed with a lang suffix).
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

    # Drop existing tracks matching the target lang (resync case)
    keep_audio_indices: list[int] | None = None
    audios = [s for s in probe.streams(target) if s.get("codec_type") == "audio"]
    if any(lang_matches(s.get("tags", {}).get("language", ""), lang) for s in audios):
        keep_audio_indices = [
            int(s["index"])
            for s in audios
            if not lang_matches(s.get("tags", {}).get("language", ""), lang)
        ]
        log.info("stripping existing %s audio; keeping audio tracks %s", lang, keep_audio_indices)

    with tempfile.TemporaryDirectory(dir=target_dir) as td:
        out_tmp = os.path.join(td, "out.mkv")
        cmd = ["mkvmerge", "-o", out_tmp]
        if keep_audio_indices:
            cmd += ["--audio-tracks", ",".join(str(i) for i in keep_audio_indices)]
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
        log.info("mkvmerge: %s", " ".join(cmd))
        t_mux = time.time()
        subprocess.run(cmd, check=True)
        log.info("mkvmerge: %.1fs", time.time() - t_mux)

        orig_size = target_p.stat().st_size
        new_size = os.path.getsize(out_tmp)
        if new_size < orig_size * 0.9:
            raise RuntimeError(f"merged file suspiciously small: {new_size} < 90% of {orig_size}")

        os.replace(out_tmp, final)

    if str(final) != str(target_p):
        target_p.unlink(missing_ok=True)
    log.info("muxed: %s (delay=%dms, mode=%s, total=%.1fs)",
             final, delay_ms, trim_mode, time.time() - t0)
    return str(final)
