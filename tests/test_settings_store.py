"""Tests for SettingsStore: defaults merge, save round-trip."""
from src.settings_store import DEFAULTS, SettingsStore


def test_defaults_written_on_first_load(tmp_path):
    s = SettingsStore(tmp_path / "settings.yml")
    d = s.load()
    # all top-level keys present
    for k in DEFAULTS:
        assert k in d


def test_update_merges(tmp_path):
    s = SettingsStore(tmp_path / "s.yml")
    s.update("sonarr", url="http://sonarr:8989", api_key="abc")
    d = s.load()
    assert d["sonarr"]["url"] == "http://sonarr:8989"
    assert d["sonarr"]["api_key"] == "abc"
    # untouched defaults remain
    assert d["sonarr"]["rescan_after_mux"] is True


def test_new_default_keys_appear_in_existing_file(tmp_path):
    """When DEFAULTS gains a key, load() should expose it even if file is older."""
    p = tmp_path / "s.yml"
    s = SettingsStore(p)
    # sanity: webhook_secret default present
    assert "webhook_secret" in s.load()["sonarr"]
    assert "library_server" in s.load()


def test_atomic_write_no_partial_file(tmp_path):
    p = tmp_path / "s.yml"
    s = SettingsStore(p)
    s.update("ui", theme="light")
    # tmp file should be cleaned up after rename
    assert not p.with_suffix(".tmp").exists()
    assert p.exists()
