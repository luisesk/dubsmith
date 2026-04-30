"""Verify Dubsmith source key -> mdnx --service mapping."""
from src.downloader import SERVICE_MAP, _service


def test_default_is_crunchy():
    assert _service(None) == "crunchy"
    assert _service("") == "crunchy"
    assert _service("crunchyroll") == "crunchy"


def test_known_sources():
    assert _service("hidive") == "hidive"
    assert _service("adn") == "ADN"
    assert _service("ADN") == "ADN"  # case-insensitive


def test_unknown_falls_back_to_crunchy():
    assert _service("netflix") == "crunchy"


def test_service_map_keys_lowercase():
    for k in SERVICE_MAP:
        assert k == k.lower()
