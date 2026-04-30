"""Wrap ffprobe to inspect container audio/sub tracks."""
import json
import subprocess


def streams(path: str) -> list[dict]:
    r = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_streams", "-of", "json",
            path,
        ],
        check=True, capture_output=True, text=True,
    )
    return json.loads(r.stdout).get("streams", [])


def audio_languages(path: str) -> list[str]:
    return [
        s.get("tags", {}).get("language", "")
        for s in streams(path) if s.get("codec_type") == "audio"
    ]


def jpn_audio_index(path: str) -> int:
    """Stream index of the first jpn audio track, or first audio if none jpn."""
    audios = [s for s in streams(path) if s.get("codec_type") == "audio"]
    for s in audios:
        if s.get("tags", {}).get("language") == "jpn":
            return int(s["index"])
    if audios:
        return int(audios[0]["index"])
    raise RuntimeError(f"no audio stream in {path}")


def por_audio_indices(path: str) -> list[int]:
    return [
        int(s["index"])
        for s in streams(path)
        if s.get("codec_type") == "audio" and s.get("tags", {}).get("language") == "por"
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
