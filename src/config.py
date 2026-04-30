"""Config loader with env-var overrides and validation."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# Env vars override YAML. Naming: DUBSMITH_<SECTION>__<KEY> (double underscore between
# section and key, single underscore preserved within multi-word keys).
# Examples:
#   DUBSMITH_API__PASSWORD              -> api.password
#   DUBSMITH_SONARR__URL                -> sonarr.url
#   DUBSMITH_SONARR__API_KEY            -> sonarr.api_key
#   DUBSMITH_LIBRARY_SERVER__TYPE       -> library_server.type
#   DUBSMITH_TARGET_LANGUAGE__AUDIO     -> target_language.audio
ENV_PREFIX = "DUBSMITH_"


def _apply_env_overrides(cfg: dict) -> dict:
    for k, v in os.environ.items():
        if not k.startswith(ENV_PREFIX):
            continue
        # Split on `__` to preserve `_` inside section/key names.
        path = [seg.lower() for seg in k[len(ENV_PREFIX):].split("__") if seg]
        if len(path) < 2:
            continue
        d = cfg
        for p in path[:-1]:
            if p not in d or not isinstance(d[p], dict):
                d[p] = {}
            d = d[p]
        d[path[-1]] = v
    return cfg


REQUIRED_PATHS = [
    ("paths", "library_in_container"),
    ("paths", "staging"),
    ("target_language", "audio"),
]


def _validate(cfg: dict) -> list[str]:
    errors = []
    for path in REQUIRED_PATHS:
        d = cfg
        for p in path:
            if not isinstance(d, dict) or p not in d:
                errors.append(f"missing config: {'.'.join(path)}")
                break
            d = d[p]
    return errors


def load(path: str | None = None) -> dict:
    p = path or os.environ.get("DUBSMITH_CONFIG") or os.environ.get("PLEX_DUB_CONFIG", "/data/config.yml")
    if not Path(p).exists():
        log.warning("config %s missing — using empty defaults", p)
        cfg = {}
    else:
        with open(p) as f:
            cfg = yaml.safe_load(f) or {}

    cfg = _apply_env_overrides(cfg)
    errors = _validate(cfg)
    if errors:
        for e in errors:
            log.error("CONFIG: %s", e)
        # Fill defaults so daemon doesn't crash; user gets clear logs
        cfg.setdefault("paths", {}).setdefault("library_in_container", "/library")
        cfg["paths"].setdefault("staging", "/data/staging")
        cfg.setdefault("target_language", {}).setdefault("audio", "por")
        cfg["target_language"].setdefault("audio_label", "Portuguese Brazil")
        cfg["target_language"].setdefault("cr_dub_lang", "por")
        cfg["target_language"].setdefault("cr_sub_lang", "por")
    return cfg


def data_dir() -> Path:
    return Path(os.environ.get("DUBSMITH_DATA") or os.environ.get("PLEX_DUB_DATA", "/data"))
