"""Find episodes missing the target dub language."""
import logging
from dataclasses import dataclass
from pathlib import Path

from . import probe
from .lang import lang_matches

log = logging.getLogger(__name__)


@dataclass
class MissingDub:
    series_id: int
    season: int
    episode: int
    target_path: str  # path inside the runtime container


def find_missing(sonarr, series_id: int, target_lang: str,
                 path_remap: tuple[str, str]) -> list[MissingDub]:
    """List eps that don't yet have target_lang (e.g. 'por') in audio tracks.

    path_remap: (sonarr_path_prefix, container_path_prefix) — Sonarr returns paths
    as it sees them (host POV); we read files from the container's mount.
    """
    sonarr_prefix, container_prefix = path_remap
    files = sonarr.episode_files(series_id)
    eps = {e["id"]: e for e in sonarr.episodes(series_id)}

    # episodefile.id -> [episode]
    file_to_eps: dict[int, list[dict]] = {}
    for e in eps.values():
        fid = e.get("episodeFileId") or 0
        if fid:
            file_to_eps.setdefault(fid, []).append(e)

    missing = []
    for f in files:
        host_path = f.get("path", "")
        local_path = host_path.replace(sonarr_prefix, container_prefix, 1)
        if not Path(local_path).exists():
            log.warning("file not found in container: %s", local_path)
            continue
        try:
            langs = probe.audio_languages(local_path)
        except Exception as e:
            log.warning("probe failed for %s: %s", local_path, e)
            continue
        if any(lang_matches(l, target_lang) for l in langs):
            continue
        for ep in file_to_eps.get(f["id"], []):
            missing.append(MissingDub(
                series_id=series_id,
                season=ep["seasonNumber"],
                episode=ep["episodeNumber"],
                target_path=local_path,
            ))
    return missing
