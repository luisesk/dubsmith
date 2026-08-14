"""Audio offset detection via FFT cross-correlation.

Two modes:
- detect(): single-window correlation (legacy).
- detect_multi_window(): split signal into N chunks, correlate each, take
  median offset. Robust to content divergence (recap intros, OP/ED differences)
  because outlier windows don't sway the median.
"""
import logging
import os
import statistics
import subprocess
import tempfile
from dataclasses import dataclass

import numpy as np
from scipy.io import wavfile
from scipy.signal import fftconvolve

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    delay_ms: int
    score: float
    # Per-window deltas if multi-window was used; empty for single.
    windows: list[int] = None


def _extract_wav(src: str, out: str, sr: int, trim_s: int,
                 map_idx: int | None = None, start_s: int = 0) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if start_s > 0:
        cmd += ["-ss", str(start_s)]
    cmd += ["-i", src]
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


def _correlate(sig_a: np.ndarray, sig_b: np.ndarray, sr_a: int,
                bound_s: int) -> tuple[int, float]:
    """Cross-correlate two signals. Returns (delay_ms, score).

    Convention: positive delay_ms means source (sig_b) needs to be PUSHED
    LATER (delayed) to align with target (sig_a). Negative means source
    needs to be PULLED EARLIER (trim leading silence).

    Math: scipy.signal.correlate(a, b) peaks at lag where a[t+lag] best
    matches b[t]. If b is delayed version of a (b's content arrives later),
    peak at lag = -delay. So delay_to_apply_to_source = -lag.
    fftconvolve(a, b[::-1]) is equivalent to correlate(a, b).
    """
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
    score = float(np.max(bounded) / (np.mean(np.abs(corr)) + 1e-9))
    return int(round((lag / sr_a) * 1000)), score


def detect_multi_window(target_path: str, target_audio_index: int,
                         source_path: str, source_audio_index: int | None = None,
                         windows: int = 4, window_s: int = 60,
                         bound_s: int = 15, sr: int = 16000) -> SyncResult:
    """Multi-window cross-correlation. Splits both signals into N windows,
    correlates each independently, returns median delay.

    Robust to content divergence: when one window's offset disagrees wildly
    with the others (e.g. recap intro present in one source), it gets
    discarded by the median.

    target_audio_index / source_audio_index: ffmpeg stream indexes for the
    audio tracks to compare. Pass the same language in both for high-accuracy
    same-lang correlation.
    """
    with tempfile.TemporaryDirectory() as td:
        per_window: list[tuple[int, float]] = []
        for i in range(windows):
            offset_s = i * window_s
            a = os.path.join(td, f"tgt_{i}.wav")
            b = os.path.join(td, f"src_{i}.wav")
            try:
                _extract_wav(target_path, a, sr=sr, trim_s=window_s,
                             map_idx=target_audio_index, start_s=offset_s)
                _extract_wav(source_path, b, sr=sr, trim_s=window_s,
                             map_idx=source_audio_index, start_s=offset_s)
            except subprocess.CalledProcessError:
                # Window past end of file — stop adding windows
                break
            try:
                sr_a, sig_a = _load(a)
                sr_b, sig_b = _load(b)
            except (FileNotFoundError, ValueError):
                continue
            if sr_a != sr_b or len(sig_a) < sr_a or len(sig_b) < sr_b:
                continue
            delay_ms, score = _correlate(sig_a, sig_b, sr_a, bound_s)
            per_window.append((delay_ms, score))

        if not per_window:
            raise RuntimeError("multi-window sync: no usable windows")

        delays = [d for d, _ in per_window]
        scores = [s for _, s in per_window]
        median_delay = int(statistics.median(delays))
        # Score: median of per-window scores (more representative than mean
        # when one window is great + others poor).
        median_score = float(statistics.median(scores))
        # Bonus: how tightly windows agree. Tight cluster = high confidence.
        spread_ms = max(delays) - min(delays) if len(delays) > 1 else 0
        log.info("multi-window sync: windows=%d delays_ms=%s spread=%dms median=%d score=%.1f",
                 len(per_window), delays, spread_ms, median_delay, median_score)
        return SyncResult(delay_ms=median_delay, score=median_score, windows=delays)


def first_speech_ms(audio_path: str, audio_index: int | None = None,
                     scan_s: int = 180, sr: int = 16000,
                     vad_aggressiveness: int = 2,
                     min_voiced_ms: int = 2000) -> int | None:
    """Return ms timestamp of the first sustained speech onset, or None.

    Uses Google's WebRTC VAD (via webrtcvad-wheels) — a real speech model,
    not just energy threshold. Distinguishes speech from music/SFX/silence.

    audio_index:
      - int: absolute ffmpeg stream index (e.g. JPN audio in target).
      - None: pick first audio stream by type (typical for source files).

    vad_aggressiveness: 0-3. Higher = more aggressive at filtering non-speech.
    min_voiced_ms: required run of consecutive voiced frames to count as onset.
    """
    try:
        import webrtcvad
    except ImportError:
        log.warning("first_speech: webrtcvad not installed; can't run VAD")
        return None

    with tempfile.TemporaryDirectory() as td:
        wav = os.path.join(td, "scan.wav")
        map_arg = f"0:{audio_index}" if audio_index is not None else "0:a:0"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", audio_path,
            "-map", map_arg,
            "-t", str(scan_s),
            "-ac", "1", "-ar", str(sr),
            "-c:a", "pcm_s16le",
            wav,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            log.warning("first_speech: ffmpeg extract failed: %s",
                        (e.stderr or b"")[:200])
            return None
        try:
            srate, data = wavfile.read(wav)
        except Exception as e:
            log.warning("first_speech: wav read failed: %s", e)
            return None
        if data.ndim > 1:
            data = data.mean(axis=1).astype(np.int16)
        if data.dtype != np.int16:
            data = data.astype(np.int16)

        # webrtcvad needs 10/20/30ms frames at 8/16/32/48kHz
        frame_ms = 30
        frame_samples = int(srate * frame_ms / 1000)
        vad = webrtcvad.Vad(vad_aggressiveness)
        need = max(1, int(min_voiced_ms / frame_ms))
        consec = 0
        n_frames = len(data) // frame_samples
        for i in range(n_frames):
            start = i * frame_samples
            chunk = data[start:start + frame_samples]
            try:
                voiced = vad.is_speech(chunk.tobytes(), srate)
            except Exception:
                continue
            if voiced:
                consec += 1
                if consec >= need:
                    # Onset = start of the consecutive run
                    onset_frame = i - (consec - 1)
                    return int(round((onset_frame * frame_samples / srate) * 1000))
            else:
                consec = 0
        return None


def first_subtitle_ms(mkv_path: str,
                       lang_priority: tuple = ("jpn", "ja", "eng", "en", "por", "pt"),
                       skip_first_n: int = 0) -> int | None:
    """Return ms timestamp of first dialogue-bearing subtitle in any sub track.

    Two-pass:
    1. Try SRT extraction (works for text subs: SRT, ASS, MOV_TEXT).
       Filters out music tags, scene descriptions, single-char markers.
    2. Fallback to packet-timestamp probe (works for PGS image subs and any
       other format). Just grabs first subtitle packet's pts_time. Less
       precise (no content filtering) but works for image-based subs.

    Picks sub track by lang priority, but timing is what matters — language
    is just for choice when multiple text tracks exist.
    """
    try:
        from . import probe
        streams = probe.streams(mkv_path)
    except Exception:
        return None
    sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    if not sub_streams:
        return None
    pri = list(lang_priority)
    def _score(s):
        lang = (s.get("tags", {}).get("language") or "").lower()
        return pri.index(lang) if lang in pri else 99
    sub_streams.sort(key=_score)

    import re
    ts_re = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->")
    text_sub_codecs = {"subrip", "ass", "ssa", "mov_text", "webvtt"}

    for s in sub_streams:
        idx = s["index"]
        codec = (s.get("codec_name") or "").lower()
        # Pass 1: text subs → SRT extract + content filter
        if codec in text_sub_codecs:
            try:
                r = subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", mkv_path,
                     "-map", f"0:{idx}", "-f", "srt", "-"],
                    capture_output=True, text=True, timeout=30,
                )
                if r.returncode == 0 and r.stdout:
                    skipped = 0
                    for block in r.stdout.split("\n\n"):
                        m = ts_re.search(block)
                        if not m:
                            continue
                        lines = block.strip().split("\n")
                        if len(lines) < 3:
                            continue
                        text = " ".join(lines[2:]).strip()
                        text = re.sub(r"<[^>]+>", "", text)
                        text = re.sub(r"\{[^}]+\}", "", text)
                        t = text.strip()
                        if not t or t in ("♪", "—", "-", "..."):
                            continue
                        if t.startswith("[") and t.endswith("]"):
                            continue
                        if t.startswith("(") and t.endswith(")"):
                            continue
                        if len(t) < 2:
                            continue
                        if skipped < skip_first_n:
                            skipped += 1
                            continue
                        h, mn, sec, ms = map(int, m.groups())
                        return h * 3600_000 + mn * 60_000 + sec * 1000 + ms
            except Exception:
                pass

        # Pass 2 (any codec, incl. image-based PGS): first packet timestamp
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-select_streams", str(idx),
                 "-show_entries", "packet=pts_time",
                 "-of", "default=noprint_wrappers=1:nokey=1",
                 "-read_intervals", "%+300",  # only scan first 300s
                 mkv_path],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0 and r.stdout:
                # First non-empty pts_time line (skip "N/A")
                skip_left = skip_first_n
                for line in r.stdout.splitlines():
                    line = line.strip()
                    if not line or line == "N/A":
                        continue
                    try:
                        ts = float(line)
                    except ValueError:
                        continue
                    if skip_left > 0:
                        skip_left -= 1
                        continue
                    return int(round(ts * 1000))
        except Exception:
            continue
    return None


def all_subtitle_starts_ms(mkv_path: str, max_scan_s: int = 1500,
                            stream_idx: int | None = None) -> list[int]:
    """Return list of subtitle start timestamps (ms) for the chosen sub track.

    Uses ffprobe -show_packets which works for ANY sub format (PGS/SRT/ASS).
    No content filter — all packets count. Used to build a density signal
    for cross-correlation alignment.
    """
    try:
        from . import probe
        streams = probe.streams(mkv_path)
    except Exception:
        return []
    sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    if not sub_streams:
        return []
    if stream_idx is None:
        # Pick first sub track (any lang). Density profile is what matters,
        # not language.
        stream_idx = sub_streams[0]["index"]
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-select_streams", str(stream_idx),
             "-show_entries", "packet=pts_time",
             "-of", "default=noprint_wrappers=1:nokey=1",
             "-read_intervals", f"%+{max_scan_s}",
             mkv_path],
            capture_output=True, text=True, timeout=120,
        )
    except Exception:
        return []
    if r.returncode != 0:
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line or line == "N/A":
            continue
        try:
            out.append(int(round(float(line) * 1000)))
        except ValueError:
            continue
    return out


def _parse_cr_chapters_txt(txt_path: str) -> list[dict]:
    """Parse mdnx-style chapter txt: CHAPTER1=00:00:00.00 / CHAPTER1NAME=Episode."""
    import re
    out: dict[int, dict] = {}
    try:
        with open(txt_path) as f:
            content = f.read()
    except Exception:
        return []
    pat_time = re.compile(r"CHAPTER(\d+)=(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?")
    pat_name = re.compile(r"CHAPTER(\d+)NAME=(.+)")
    for m in pat_time.finditer(content):
        idx = int(m.group(1))
        h, mm, ss = int(m.group(2)), int(m.group(3)), int(m.group(4))
        frac = m.group(5) or "0"
        ms = int((float("0." + frac)) * 1000)
        out.setdefault(idx, {})["start_ms"] = h * 3600_000 + mm * 60_000 + ss * 1000 + ms
    for m in pat_name.finditer(content):
        idx = int(m.group(1))
        out.setdefault(idx, {})["title"] = m.group(2).strip()
    return [out[k] for k in sorted(out.keys()) if "start_ms" in out[k]]


def _read_target_chapters(mkv_path: str) -> list[dict]:
    """Read embedded chapters from a video container via ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters",
             "-of", "json", mkv_path],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return []
    if r.returncode != 0:
        return []
    try:
        data = __import__("json").loads(r.stdout)
    except Exception:
        return []
    chapters = data.get("chapters", []) or []
    out = []
    for c in chapters:
        try:
            start_s = float(c.get("start_time", 0))
        except (TypeError, ValueError):
            continue
        out.append({
            "start_ms": int(round(start_s * 1000)),
            "title": (c.get("tags") or {}).get("title", ""),
        })
    return out


def detect_chapter_offset(target_path: str,
                           source_path: str) -> SyncResult:
    """Sync via chapter alignment.

    Both CR (mdnx muxes them in) and Bluray rips (extracted from BD)
    typically have chapter markers at the same content boundaries
    (cold-open, intro, main, credits). Read both, pair by ordinal,
    take median delta = robust content-aware offset.

    No OCR, no text matching, no audio analysis. Just chapter timestamps.
    """
    cr_chs = _read_target_chapters(source_path)
    tgt_chs = _read_target_chapters(target_path)
    if not cr_chs:
        raise RuntimeError(f"chapter sync: source has no chapters")
    if not tgt_chs:
        raise RuntimeError("chapter sync: target has no chapters")
    if len(cr_chs) < 2 or len(tgt_chs) < 2:
        raise RuntimeError(f"chapter sync: too few chapters (cr={len(cr_chs)}, tgt={len(tgt_chs)})")

    # Pair by ordinal; minimum count
    n = min(len(cr_chs), len(tgt_chs))
    deltas = []
    for i in range(n):
        delta = tgt_chs[i]["start_ms"] - cr_chs[i]["start_ms"]
        deltas.append(delta)

    # Outlier filter: chapters often diverge once one source has more chapters
    # than the other, or after a season-specific bumper. Use Median Absolute
    # Deviation (MAD) — drop any delta > 30s away from initial median.
    # Then recompute median on the consensus subset.
    initial_median = statistics.median(deltas)
    OUTLIER_THRESHOLD_MS = 30_000
    consensus = [d for d in deltas if abs(d - initial_median) <= OUTLIER_THRESHOLD_MS]
    if len(consensus) < 2:
        # All deltas disagree — chapter sets aren't comparable
        raise RuntimeError(f"chapter sync: no consensus among {len(deltas)} deltas: {deltas}")
    median_delta = int(statistics.median(consensus))
    consensus_spread = max(consensus) - min(consensus)
    score = max(0.0, 100.0 - consensus_spread / 1000.0)
    log.info("chapter sync: cr_ch=%d tgt_ch=%d all_deltas=%s consensus=%s "
             "spread=%dms median=%dms score=%.1f",
             len(cr_chs), len(tgt_chs), deltas, consensus,
             consensus_spread, median_delta, score)
    return SyncResult(delay_ms=median_delta, score=score, windows=consensus)


def all_speech_starts_ms(audio_path: str, audio_index: int | None = None,
                          scan_s: int = 600, sr: int = 16000,
                          vad_aggressiveness: int = 2,
                          min_voiced_ms: int = 240,
                          min_gap_ms: int = 500) -> list[int]:
    """Return list of speech-segment start timestamps via WebRTC VAD.

    Walks 30ms frames. A "speech segment" starts when min_voiced_ms of
    consecutive voiced frames begin, ends when min_gap_ms of silence
    follows. Returns onset of each segment.

    This builds a dialogue-density signal across the entire scan window
    that's what density cross-correlation needs — content-agnostic
    timing pattern of when characters speak.
    """
    try:
        import webrtcvad
    except ImportError:
        log.warning("all_speech_starts_ms: webrtcvad not installed")
        return []
    with tempfile.TemporaryDirectory() as td:
        wav = os.path.join(td, "scan.wav")
        map_arg = f"0:{audio_index}" if audio_index is not None else "0:a:0"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", audio_path,
            "-map", map_arg,
            "-t", str(scan_s),
            "-ac", "1", "-ar", str(sr),
            "-c:a", "pcm_s16le",
            wav,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError:
            return []
        try:
            srate, data = wavfile.read(wav)
        except Exception:
            return []
        if data.ndim > 1:
            data = data.mean(axis=1).astype(np.int16)
        if data.dtype != np.int16:
            data = data.astype(np.int16)
        frame_ms = 30
        frame_samples = int(srate * frame_ms / 1000)
        vad = webrtcvad.Vad(vad_aggressiveness)
        need_voiced = max(1, int(min_voiced_ms / frame_ms))
        need_silence = max(1, int(min_gap_ms / frame_ms))
        starts: list[int] = []
        in_segment = False
        consec_voiced = 0
        consec_silence = 0
        n_frames = len(data) // frame_samples
        for i in range(n_frames):
            chunk = data[i * frame_samples:(i + 1) * frame_samples]
            try:
                voiced = vad.is_speech(chunk.tobytes(), srate)
            except Exception:
                voiced = False
            if not in_segment:
                if voiced:
                    consec_voiced += 1
                    if consec_voiced >= need_voiced:
                        seg_start_frame = i - need_voiced + 1
                        starts.append(int(round(seg_start_frame * frame_samples / srate * 1000)))
                        in_segment = True
                        consec_voiced = 0
                        consec_silence = 0
                else:
                    consec_voiced = 0
            else:
                if voiced:
                    consec_silence = 0
                else:
                    consec_silence += 1
                    if consec_silence >= need_silence:
                        in_segment = False
                        consec_voiced = 0
        return starts


def detect_audio_density_offset(target_path: str, target_audio_index: int,
                                  source_path: str,
                                  bin_ms: int = 1000,
                                  bound_s: int = 600,
                                  scan_s: int = 600) -> SyncResult:
    """Audio VAD density cross-correlation. Same algorithm as ffsubsync.

    1. VAD-detect speech onsets in both files (full scan window)
    2. Bin into per-second density signals
    3. Cross-correlate → peak = global offset
    """
    log.info("audio-density: detecting speech in target")
    t = all_speech_starts_ms(target_path, audio_index=target_audio_index,
                              scan_s=scan_s)
    log.info("audio-density: detecting speech in source")
    s = all_speech_starts_ms(source_path, audio_index=None, scan_s=scan_s)
    if not t or not s:
        raise RuntimeError(f"audio-density: no speech detected (target={len(t)}, source={len(s)})")

    duration_ms = max(max(t), max(s)) + 60_000
    n_bins = duration_ms // bin_ms + 1
    a = np.zeros(n_bins, dtype=np.float32)
    b = np.zeros(n_bins, dtype=np.float32)
    for ts in t:
        a[ts // bin_ms] = 1.0
    for ts in s:
        b[ts // bin_ms] = 1.0
    corr = fftconvolve(a, b[::-1], mode="full")
    center = n_bins - 1
    max_lag_bins = (bound_s * 1000) // bin_ms
    start = max(0, center - max_lag_bins)
    end = min(len(corr), center + max_lag_bins + 1)
    bounded = corr[start:end]
    peak = int(np.argmax(bounded)) + start
    lag_bins = peak - center
    delay_ms = lag_bins * bin_ms
    score = float(np.max(bounded) / (np.mean(corr) + 1e-9))
    log.info("audio-density sync: target_speech=%d source_speech=%d "
             "lag_bins=%d → delay=%dms score=%.1f",
             len(t), len(s), lag_bins, delay_ms, score)
    return SyncResult(delay_ms=delay_ms, score=score)


def detect_subtitle_density_offset(target_path: str, source_path: str,
                                     bin_ms: int = 1000,
                                     bound_s: int = 600) -> SyncResult:
    """Align by cross-correlating subtitle DENSITY profiles.

    Builds a binary per-second signal for each file (1 = sub starts that
    second). Cross-correlates the two — peak = global offset that best
    aligns the entire dialogue timeline.

    Robust against:
    - Different sub languages (timing-only)
    - PGS vs SRT vs ASS (uses packet timestamps, format-agnostic)
    - Single-anchor mismatches (uses full-file pattern, not first event)
    - Asymmetric subbing (OP not subbed in one, subbed in other) — the
      MAIN content density still cross-correlates cleanly

    bound_s: max search range each direction (default ±10 min).
    """
    t = all_subtitle_starts_ms(target_path)
    s = all_subtitle_starts_ms(source_path)
    if not t:
        raise RuntimeError(f"sub-density: target has no sub timestamps")
    if not s:
        raise RuntimeError(f"sub-density: source has no sub timestamps")

    # Cap to longest sub timestamp + buffer
    duration_ms = max(max(t), max(s)) + 60_000
    n_bins = duration_ms // bin_ms + 1
    a = np.zeros(n_bins, dtype=np.float32)
    b = np.zeros(n_bins, dtype=np.float32)
    for ts in t:
        a[ts // bin_ms] = 1.0
    for ts in s:
        b[ts // bin_ms] = 1.0

    # Cross-correlate. Peak at lag = bins source needs shift relative to target
    corr = fftconvolve(a, b[::-1], mode="full")
    center = n_bins - 1
    max_lag_bins = (bound_s * 1000) // bin_ms
    start = max(0, center - max_lag_bins)
    end = min(len(corr), center + max_lag_bins + 1)
    bounded = corr[start:end]
    peak = int(np.argmax(bounded)) + start
    lag_bins = peak - center
    # If a (target) has its events `lag_bins` later than b (source), peak at
    # +lag_bins. Source needs +lag_bins delay to align with target.
    delay_ms = lag_bins * bin_ms
    score = float(np.max(bounded) / (np.mean(corr) + 1e-9))
    log.info("sub-density sync: target_subs=%d source_subs=%d "
             "lag_bins=%d → delay=%dms score=%.1f",
             len(t), len(s), lag_bins, delay_ms, score)
    return SyncResult(delay_ms=delay_ms, score=score)


def detect_subtitle_offset(target_path: str, source_path: str) -> SyncResult:
    """Align by first dialogue subtitle timestamp in each file.

    delay_ms = target_first_sub - source_first_sub
      Source needs to be delayed by that amount so its first dialogue
      lands at target's first dialogue position.

    Works when both files have embedded subs (common for anime Bluray +
    CR's mdnx output with --dlsubs). Cleaner than VAD because subs are
    explicit dialogue annotations.
    """
    t = first_subtitle_ms(target_path)
    s = first_subtitle_ms(source_path)
    if t is None or s is None:
        raise RuntimeError(f"sub-sync: missing subs (target={t}, source={s})")
    delay_ms = t - s
    log.info("subtitle sync: target_first_sub=%dms source_first_sub=%dms → delay=%dms",
             t, s, delay_ms)
    return SyncResult(delay_ms=delay_ms, score=99.0)


def detect_first_speech_offset(target_path: str, target_audio_index: int,
                                source_path: str, source_audio_index: int | None = None,
                                scan_s: int = 120) -> SyncResult:
    """VAD-based sync: align first speech onset in source with target.

    delay_ms = target_first_speech - source_first_speech
      If source has more leading silence than target → negative (trim source)
      If target has more leading silence than source → positive (delay source)

    Score: distance between detected onsets in seconds (lower is more
    confident — both files have a clean first-speech onset within a few sec).
    Score = 100 - abs(delta_seconds) capped at [0, 100].
    """
    t0 = first_speech_ms(target_path, target_audio_index, scan_s=scan_s)
    s0 = first_speech_ms(source_path, source_audio_index, scan_s=scan_s)
    if t0 is None or s0 is None:
        raise RuntimeError(f"VAD: no speech detected (target={t0}, source={s0})")
    delay_ms = t0 - s0
    score = max(0.0, 100.0 - abs(delay_ms) / 1000.0)
    log.info("VAD sync: target_first_speech=%dms source_first_speech=%dms → delay=%dms",
             t0, s0, delay_ms)
    return SyncResult(delay_ms=delay_ms, score=score)


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
