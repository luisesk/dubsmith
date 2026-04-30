"""Mux a target-language audio track into an existing video container.

Two paths depending on delay sign:
- delay_ms >= 0: lossless. mkvmerge --sync 0:N applies positive delay via container
  metadata (Plex honors this for non-encrypted tracks; if Plex strips it again the
  fallback is to ffmpeg-pad like the negative-delay branch).
- delay_ms < 0: ffmpeg trims the source's leading audio (atrim) — re-encode required
  because we change PTS — then mkvmerge muxes the trimmed track.
"""
import logging
import os
import subprocess
import tempfile
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


def _filename_suffix(lang: str) -> str:
    return _LANG_SUFFIX.get(lang, f"Dub-{lang}")


def _trim_audio(src: str, out: str, delay_ms: int) -> None:
    """ffmpeg trim leading audio for negative delay. Re-encodes to AAC 192k."""
    if delay_ms >= 0:
        raise ValueError("_trim_audio only handles negative delays")
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


def inject(target: str, source: str, delay_ms: int,
           lang: str = "por", track_name: str = "Portuguese Brazil") -> str:
    """Mux source audio into target. Strips any pre-existing track of the same lang
    (resync case). Returns path to final file (may be renamed with a lang suffix).
    """
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

        if delay_ms >= 0:
            # Lossless path: container --sync metadata. mkvmerge applies it on the
            # next input file's tracks (the source we're appending).
            audio_in = source
            sync_arg = ["--sync", f"0:{delay_ms}"]
        else:
            # Negative delay requires PTS rewrite -> re-encode.
            audio_in = os.path.join(td, "trimmed.mkv")
            _trim_audio(source, audio_in, delay_ms)
            sync_arg = []

        cmd += sync_arg + [
            "--no-video", "--no-attachments", "--no-chapters", "--no-buttons", "--no-track-tags",
            "--language", f"0:{lang}",
            "--track-name", f"0:{track_name}",
            "--default-track-flag", "0:0",
            audio_in,
        ]
        log.info("mkvmerge: %s", " ".join(cmd))
        subprocess.run(cmd, check=True)

        orig_size = target_p.stat().st_size
        new_size = os.path.getsize(out_tmp)
        if new_size < orig_size * 0.9:
            raise RuntimeError(f"merged file suspiciously small: {new_size} < 90% of {orig_size}")

        os.replace(out_tmp, final)

    if str(final) != str(target_p):
        target_p.unlink(missing_ok=True)
    log.info("muxed: %s (delay=%dms, lossless=%s)", final, delay_ms, delay_ms >= 0)
    return str(final)
