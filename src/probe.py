"""Wrap ffprobe to inspect container audio/sub tracks. Output cached by (path, mtime, size)."""
import json
import os
import subprocess
import threading
import time

from .lang import lang_matches, normalize

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, int, int], tuple[float, list[dict]]] = {}
_CACHE_TTL = 3600.0  # 1h
_CACHE_MAX = 5000


def _cache_key(path: str) -> tuple[str, int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (path, int(st.st_mtime), st.st_size)


def streams(path: str, no_cache: bool = False) -> list[dict]:
    key = _cache_key(path)
    now = time.time()
    if key and not no_cache:
        with _CACHE_LOCK:
            v = _CACHE.get(key)
            if v and now - v[0] < _CACHE_TTL:
                return v[1]
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", path],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        # Surface the actual ffprobe error instead of "non-zero exit status 1"
        err = (r.stderr or "").strip().splitlines()[-1] if r.stderr else "unknown"
        raise RuntimeError(f"ffprobe failed on {path!r}: {err[:240]}")
    try:
        out = json.loads(r.stdout).get("streams", [])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ffprobe produced invalid JSON for {path!r}: {e}")
    if key:
        with _CACHE_LOCK:
            if len(_CACHE) >= _CACHE_MAX:
                # evict oldest ~10%
                drop = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[: _CACHE_MAX // 10]
                for k, _ in drop:
                    _CACHE.pop(k, None)
            _CACHE[key] = (now, out)
    return out


def cache_stats() -> dict:
    with _CACHE_LOCK:
        return {"entries": len(_CACHE), "max": _CACHE_MAX, "ttl_s": _CACHE_TTL}


def audio_languages(path: str) -> list[str]:
    """Raw language tags as ffprobe sees them (no normalization)."""
    return [
        s.get("tags", {}).get("language", "")
        for s in streams(path) if s.get("codec_type") == "audio"
    ]


def has_audio_lang(path: str, lang: str) -> bool:
    """True when any audio track normalizes to the same lang as `lang`."""
    target = normalize(lang)
    if not target:
        return False
    for raw in audio_languages(path):
        if lang_matches(raw, target):
            return True
    return False


def jpn_audio_index(path: str) -> int:
    """Stream index of the first jpn audio track, or first audio if none jpn."""
    audios = [s for s in streams(path) if s.get("codec_type") == "audio"]
    for s in audios:
        if lang_matches(s.get("tags", {}).get("language", ""), "jpn"):
            return int(s["index"])
    if audios:
        return int(audios[0]["index"])
    raise RuntimeError(f"no audio stream in {path}")


def audio_indices(path: str, lang: str) -> list[int]:
    """Stream indices of audio tracks matching `lang` (cross-639-1/2)."""
    return [
        int(s["index"])
        for s in streams(path)
        if s.get("codec_type") == "audio"
        and lang_matches(s.get("tags", {}).get("language", ""), lang)
    ]


def duration_seconds(path: str) -> float:
    r = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            path,
        ],
        check=True, capture_output=True, text=True,
    )
    return float(r.stdout.strip())
