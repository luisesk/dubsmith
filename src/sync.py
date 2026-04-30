"""Audio offset detection via FFT cross-correlation. Port of scripts/detect_offset.py."""
import os
import subprocess
import tempfile
from dataclasses import dataclass

import numpy as np
from scipy.io import wavfile
from scipy.signal import fftconvolve


@dataclass
class SyncResult:
    delay_ms: int
    score: float


def _extract_wav(src: str, out: str, sr: int, trim_s: int, map_idx: int | None = None) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src]
    if map_idx is not None:
        cmd += ["-map", f"0:{map_idx}"]
    cmd += ["-t", str(trim_s), "-ac", "1", "-ar", str(sr), out]
    subprocess.run(cmd, check=True)


def _load(path: str) -> tuple[int, np.ndarray]:
    sr, data = wavfile.read(path)
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.astype(np.float32)
    data = data / (np.max(np.abs(data)) + 1e-9)
    return sr, data


def detect(target_path: str, target_jpn_index: int, source_path: str,
           trim_s: int = 120, bound_s: int = 15, sr: int = 8000) -> SyncResult:
    """Compute delay (ms) source needs to align to target jpn track. Positive = delay source."""
    with tempfile.TemporaryDirectory() as td:
        a = os.path.join(td, "tgt.wav")
        b = os.path.join(td, "src.wav")
        _extract_wav(target_path, a, sr=sr, trim_s=trim_s, map_idx=target_jpn_index)
        _extract_wav(source_path, b, sr=sr, trim_s=trim_s)
        sr_a, sig_a = _load(a)
        sr_b, sig_b = _load(b)
        if sr_a != sr_b:
            raise RuntimeError(f"sample rate mismatch: {sr_a} vs {sr_b}")
        n = max(len(sig_a), len(sig_b))
        sig_a = np.pad(sig_a, (0, n - len(sig_a)))
        sig_b = np.pad(sig_b, (0, n - len(sig_b)))
        corr = fftconvolve(sig_a, sig_b[::-1], mode="full")
        center = n - 1
        max_lag = int(bound_s * sr_a)
        start = max(0, center - max_lag)
        end = min(len(corr), center + max_lag + 1)
        bounded = corr[start:end]
        peak = int(np.argmax(bounded)) + start
        lag = peak - center
        offset_s = lag / sr_a
        score = float(np.max(bounded) / (np.mean(np.abs(corr)) + 1e-9))
        return SyncResult(delay_ms=int(round(offset_s * 1000)), score=score)
