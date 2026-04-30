"""Trim-path selection logic for negative delays."""
import subprocess
from unittest.mock import patch

import pytest

from src import mux


def test_audio_codec_returns_lower(monkeypatch):
    monkeypatch.setattr(mux.probe, "streams", lambda p: [
        {"codec_type": "video", "codec_name": "h264"},
        {"codec_type": "audio", "codec_name": "AAC"},
    ])
    assert mux._audio_codec("x") == "aac"


def test_audio_codec_no_audio(monkeypatch):
    monkeypatch.setattr(mux.probe, "streams", lambda p: [
        {"codec_type": "video", "codec_name": "h264"},
    ])
    assert mux._audio_codec("x") == ""


def test_audio_codec_probe_failure_returns_empty(monkeypatch):
    def boom(p):
        raise RuntimeError("ffprobe down")
    monkeypatch.setattr(mux.probe, "streams", boom)
    assert mux._audio_codec("x") == ""


def test_trim_picks_copy_for_aac(monkeypatch, tmp_path):
    """AAC source should hit the codec-copy fast path."""
    monkeypatch.setattr(mux, "_audio_codec", lambda p: "aac")
    called = []

    def fake_copy(src, out, delay_ms):
        called.append("copy")
        # mimic ffmpeg writing the file
        open(out, "wb").close()

    def fake_reencode(src, out, delay_ms):
        called.append("reencode")
        open(out, "wb").close()

    monkeypatch.setattr(mux, "_trim_audio_copy", fake_copy)
    monkeypatch.setattr(mux, "_trim_audio_reencode", fake_reencode)

    mode = mux._trim_audio("/in", str(tmp_path / "out.mkv"), -300)
    assert called == ["copy"]
    assert "copy" in mode
    assert "aac" in mode


def test_trim_picks_copy_for_known_codecs(monkeypatch, tmp_path):
    for codec in ("ac3", "eac3", "opus", "vorbis", "flac", "mp3"):
        monkeypatch.setattr(mux, "_audio_codec", lambda p, c=codec: c)
        called = []
        monkeypatch.setattr(mux, "_trim_audio_copy",
                            lambda *a, **k: called.append("copy") or open(a[1], "wb").close())
        monkeypatch.setattr(mux, "_trim_audio_reencode",
                            lambda *a, **k: called.append("reencode") or open(a[1], "wb").close())
        mode = mux._trim_audio("/in", str(tmp_path / f"o-{codec}.mkv"), -100)
        assert called == ["copy"], f"{codec} should have used copy path"
        assert "copy" in mode


def test_trim_falls_back_to_reencode_on_unknown_codec(monkeypatch, tmp_path):
    monkeypatch.setattr(mux, "_audio_codec", lambda p: "alac")
    called = []
    monkeypatch.setattr(mux, "_trim_audio_copy",
                        lambda *a, **k: called.append("copy"))
    monkeypatch.setattr(mux, "_trim_audio_reencode",
                        lambda *a, **k: called.append("reencode"))
    mode = mux._trim_audio("/in", str(tmp_path / "o.mkv"), -100)
    assert called == ["reencode"]
    assert "reencode" in mode


def test_trim_falls_back_to_reencode_on_copy_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(mux, "_audio_codec", lambda p: "aac")
    called = []

    def fake_copy(src, out, delay_ms):
        called.append("copy")
        raise subprocess.CalledProcessError(1, "ffmpeg")

    def fake_reencode(src, out, delay_ms):
        called.append("reencode")
        open(out, "wb").close()

    monkeypatch.setattr(mux, "_trim_audio_copy", fake_copy)
    monkeypatch.setattr(mux, "_trim_audio_reencode", fake_reencode)

    mode = mux._trim_audio("/in", str(tmp_path / "o.mkv"), -100)
    assert called == ["copy", "reencode"]
    assert "reencode" in mode


def test_trim_audio_rejects_positive_delay():
    with pytest.raises(ValueError):
        mux._trim_audio("/in", "/out", 100)


def test_filename_suffix_known_lang():
    assert mux._filename_suffix("por") == "Dublado-PT-BR"
    assert mux._filename_suffix("eng") == "Dub-EN"


def test_filename_suffix_unknown_lang():
    assert mux._filename_suffix("xxx") == "Dub-xxx"
