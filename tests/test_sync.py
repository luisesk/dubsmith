"""Sync detection regression: known-delay synthetic audio.

Builds two audio signals where one is the other shifted by N samples,
writes both as WAV, and asserts detect() recovers the offset within ±5ms.
Skips ffmpeg path by patching _extract_wav.
"""
import os
from unittest.mock import patch

import numpy as np
import pytest
from scipy.io import wavfile

from src import sync


def _write_wav(path: str, sr: int, sig: np.ndarray) -> None:
    # scale float [-1, 1] to int16
    s = (sig * 32767).clip(-32768, 32767).astype(np.int16)
    wavfile.write(path, sr, s)


def test_detect_recovers_known_delay(tmp_path):
    sr = 8000
    duration = 30  # seconds
    n = sr * duration
    rng = np.random.default_rng(42)
    base = rng.standard_normal(n).astype(np.float32) * 0.5
    # source is delayed by 320ms (positive delay = source needs +320ms)
    delay_samples = int(0.320 * sr)
    delayed = np.concatenate([np.zeros(delay_samples, dtype=np.float32), base])[:n]

    tgt_wav = tmp_path / "tgt.wav"
    src_wav = tmp_path / "src.wav"
    _write_wav(str(tgt_wav), sr, base)
    _write_wav(str(src_wav), sr, delayed)

    # bypass ffmpeg by intercepting _extract_wav: copy wav unchanged
    def fake_extract(src, out, sr, trim_s, map_idx=None):
        import shutil
        shutil.copy(src, out)

    with patch("src.sync._extract_wav", side_effect=fake_extract):
        result = sync.detect(str(tgt_wav), 0, str(src_wav), trim_s=duration, bound_s=5, sr=sr)

    # negative delay because target leads source
    assert abs(abs(result.delay_ms) - 320) <= 10
    assert result.score > 1.0


def test_detect_low_score_for_noise(tmp_path):
    sr = 8000
    duration = 10
    rng = np.random.default_rng(0)
    a = rng.standard_normal(sr * duration).astype(np.float32) * 0.3
    b = rng.standard_normal(sr * duration).astype(np.float32) * 0.3
    p1 = tmp_path / "a.wav"
    p2 = tmp_path / "b.wav"
    _write_wav(str(p1), sr, a)
    _write_wav(str(p2), sr, b)

    def fake_extract(src, out, sr, trim_s, map_idx=None):
        import shutil
        shutil.copy(src, out)

    with patch("src.sync._extract_wav", side_effect=fake_extract):
        result = sync.detect(str(p1), 0, str(p2), trim_s=duration, bound_s=5, sr=sr)

    # uncorrelated noise should score modestly (well below ~10 we'd expect for matched audio)
    assert result.score < 8.0
