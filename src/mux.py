"""Mux pt-BR audio into target via mkvmerge with pre-padded silence (or trim) for offset.

Port of scripts/inject_padded.sh — Plex strips audio start_pts so real silence prepend
required, not just mkvmerge --sync metadata.
"""
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from . import probe

log = logging.getLogger(__name__)


def _pad_audio(src: str, out: str, delay_ms: int) -> None:
    """ffmpeg apply adelay (positive) or atrim (negative) and re-encode to AAC."""
    if delay_ms >= 0:
        af = f"adelay={delay_ms}|{delay_ms}"
    else:
        trim_s = abs(delay_ms) / 1000
        af = f"atrim=start={trim_s:.3f},asetpts=PTS-STARTPTS"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", src,
            "-vn", "-sn",  # audio only — skip video and subs from source
            "-af", af,
            "-c:a", "aac", "-b:a", "192k",
            out,
        ],
        check=True,
    )


def inject(target: str, source: str, delay_ms: int,
           lang: str = "por", track_name: str = "Portuguese Brazil") -> str:
    """Mux source audio into target. If target already has 'por' audio, drop it first.

    Returns path to final file (may differ from target if 'Dublado-PT-BR' suffix added).
    """
    target_p = Path(target)
    target_dir = target_p.parent
    stem = target_p.stem
    if "Dublado-PT-BR" in stem:
        new_name = stem + ".mkv"
    else:
        new_name = f"{stem} Dublado-PT-BR.mkv"
    final = target_dir / new_name

    # Drop existing por audio tracks (resync case)
    keep_audio_indices: list[int] | None = None
    audios = [s for s in probe.streams(target) if s.get("codec_type") == "audio"]
    if any(s.get("tags", {}).get("language") == "por" for s in audios):
        keep_audio_indices = [
            int(s["index"]) for s in audios if s.get("tags", {}).get("language") != "por"
        ]
        log.info("stripping existing por audio; keeping audio tracks %s", keep_audio_indices)

    with tempfile.TemporaryDirectory(dir=target_dir) as td:
        padded = os.path.join(td, "padded.mkv")
        _pad_audio(source, padded, delay_ms)

        out_tmp = os.path.join(td, "out.mkv")
        cmd = ["mkvmerge", "-o", out_tmp]
        if keep_audio_indices:
            cmd += ["--audio-tracks", ",".join(str(i) for i in keep_audio_indices)]
        cmd += [
            target,
            "--no-video", "--no-attachments", "--no-chapters", "--no-buttons", "--no-track-tags",
            "--language", f"0:{lang}",
            "--track-name", f"0:{track_name}",
            "--default-track-flag", "0:0",
            padded,
        ]
        subprocess.run(cmd, check=True)

        orig_size = target_p.stat().st_size
        new_size = os.path.getsize(out_tmp)
        if new_size < orig_size * 0.9:
            raise RuntimeError(f"merged file suspiciously small: {new_size} < 90% of {orig_size}")

        # Atomic replace
        os.replace(out_tmp, final)

    if str(final) != str(target_p):
        target_p.unlink(missing_ok=True)
    log.info("muxed: %s (delay=%dms)", final, delay_ms)
    return str(final)
