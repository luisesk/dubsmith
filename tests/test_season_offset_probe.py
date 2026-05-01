"""probe_season_first_ep: parse mdnx season listing for lowest episode number."""
from unittest.mock import patch

from src import downloader


_LISTING_S2 = """\
=== Multi Downloader NX 5.7.2 ===

USER: someone
Your Country: BR

[S:GYX0C40MZ] Season 2 (Season: 2) [SUB, DUB]
  - Versions: en, pt-BR, ja
  - Subtitles: en
  [13|E:GN7UD2GJ1] ☆ 13 - The White Reaper Descends [23m40s, SUB, DUB, STREAM]
    - Versions: en, pt-BR, ja
  [14|E:G7PU3PXN1] ☆ 14 - The 4x4 Attack [23m40s, SUB, DUB, STREAM]
  [15|E:G9DU9Z1V0] ☆ 15 - The Relay Straight Yin-Yang [23m40s, SUB, DUB, STREAM]
"""

_LISTING_S1 = """\
USER: someone

[S:GR9PC2J2D] Season 1 (Season: 1) [SUB, DUB]
  [01|E:G9DUEGNEK] ☆ 1 - To Another World [23m60s, SUB, DUB]
  [02|E:GJWU2E1VK] ☆ 2 - Ousei Academy [23m60s, SUB, DUB]
"""


def _fake_run(stdout: str):
    class R:
        def __init__(self):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0
    return R()


def test_returns_first_ep_for_continuation_cour(monkeypatch):
    monkeypatch.setattr(downloader.subprocess, "run",
                        lambda *a, **k: _fake_run(_LISTING_S2))
    assert downloader.probe_season_first_ep("GYX0C40MZ") == 13


def test_returns_1_for_normal_season(monkeypatch):
    monkeypatch.setattr(downloader.subprocess, "run",
                        lambda *a, **k: _fake_run(_LISTING_S1))
    assert downloader.probe_season_first_ep("GR9PC2J2D") == 1


def test_returns_none_when_no_episodes_found(monkeypatch):
    monkeypatch.setattr(downloader.subprocess, "run",
                        lambda *a, **k: _fake_run("USER: x\n\nno episodes here\n"))
    assert downloader.probe_season_first_ep("GR9PC2J2D") is None


def test_rejects_invalid_cr_id():
    # security.valid_cr_id rejects shell metachars
    assert downloader.probe_season_first_ep("foo; rm -rf /") is None


def test_handles_subprocess_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("aniDL not found")
    monkeypatch.setattr(downloader.subprocess, "run", boom)
    assert downloader.probe_season_first_ep("GYX0C40MZ") is None


def test_picks_min_when_listing_unsorted(monkeypatch):
    """Defensive: even if mdnx ever returns episodes out of order, take min."""
    out = "[S:X] Season 2\n  [25|E:A]\n  [13|E:B]\n  [14|E:C]\n"
    monkeypatch.setattr(downloader.subprocess, "run",
                        lambda *a, **k: _fake_run(out))
    assert downloader.probe_season_first_ep("GYX0C40MZ") == 13


def test_compute_season_offsets_skips_normal_seasons(monkeypatch):
    """Seasons starting at ep 1 produce offset=0 → omitted from result."""
    def fake(cr_id, **kw):
        return {"GR9PC2J2D": 1, "GYX0C40MZ": 13, "GR75CDM58": 25}.get(cr_id)
    monkeypatch.setattr(downloader, "probe_season_first_ep", fake)
    out = downloader.compute_season_offsets({
        "1": "GR9PC2J2D",
        "2": "GYX0C40MZ",
        "3": "GR75CDM58",
    })
    assert out == {"2": 12, "3": 24}


def test_compute_season_offsets_handles_probe_failure(monkeypatch):
    """A None probe (network/parse error) drops that season — leaves it
    for the worker's runtime auto-recovery to handle later."""
    monkeypatch.setattr(downloader, "probe_season_first_ep", lambda *a, **k: None)
    out = downloader.compute_season_offsets({"1": "X", "2": "Y"})
    assert out == {}


def test_compute_season_offsets_empty_input():
    assert downloader.compute_season_offsets({}) == {}
    assert downloader.compute_season_offsets(None) == {}
