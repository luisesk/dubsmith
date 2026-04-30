"""Tests for config loader: env overrides + validation."""
import textwrap

from src import config


def test_env_override_simple(tmp_path, monkeypatch):
    p = tmp_path / "c.yml"
    p.write_text(textwrap.dedent("""\
        paths: {library_in_container: /lib, staging: /tmp/stg}
        target_language: {audio: por, audio_label: PT, cr_dub_lang: por, cr_sub_lang: por}
        sonarr: {url: http://old, api_key: oldkey}
    """))
    monkeypatch.setenv("DUBSMITH_SONARR__URL", "http://new:8989")
    cfg = config.load(str(p))
    assert cfg["sonarr"]["url"] == "http://new:8989"
    assert cfg["sonarr"]["api_key"] == "oldkey"


def test_env_override_preserves_underscore_in_key(tmp_path, monkeypatch):
    p = tmp_path / "c.yml"
    p.write_text("paths: {library_in_container: /l, staging: /s}\ntarget_language: {audio: por}\nsonarr: {url: ''}\n")
    monkeypatch.setenv("DUBSMITH_SONARR__API_KEY", "abc123")
    cfg = config.load(str(p))
    assert cfg["sonarr"]["api_key"] == "abc123"


def test_env_override_nested(tmp_path, monkeypatch):
    p = tmp_path / "c.yml"
    p.write_text("paths: {library_in_container: /lib, staging: /s}\ntarget_language: {audio: por}\n")
    monkeypatch.setenv("DUBSMITH_TARGET_LANGUAGE__AUDIO", "spa")
    cfg = config.load(str(p))
    assert cfg["target_language"]["audio"] == "spa"


def test_validation_fills_defaults_when_missing(tmp_path, caplog):
    p = tmp_path / "c.yml"
    p.write_text("{}\n")  # empty
    cfg = config.load(str(p))
    assert "paths" in cfg
    assert cfg["target_language"]["audio"] == "por"


def test_missing_config_file_uses_defaults(tmp_path):
    cfg = config.load(str(tmp_path / "does-not-exist.yml"))
    # Validation kicks in -> defaults filled
    assert cfg["target_language"]["audio"] == "por"


def test_env_creates_new_section(tmp_path, monkeypatch):
    p = tmp_path / "c.yml"
    p.write_text("paths: {library_in_container: /l, staging: /s}\ntarget_language: {audio: por}\n")
    monkeypatch.setenv("DUBSMITH_LIBRARY_SERVER__URL", "http://plex:32400")
    cfg = config.load(str(p))
    assert cfg["library_server"]["url"] == "http://plex:32400"
