"""Alert store + CR health probe parser."""
from unittest.mock import patch

from src import alerts, health


def setup_function(_fn):
    # reset alert store between tests
    alerts._STORE.clear()


def test_set_and_list():
    alerts.set_alert("k1", severity="warn", title="T1", message="m1")
    out = alerts.list_alerts()
    assert len(out) == 1
    assert out[0]["title"] == "T1"
    assert out[0]["severity"] == "warn"


def test_replace_same_key():
    alerts.set_alert("k", severity="warn", title="A", message="x")
    alerts.set_alert("k", severity="error", title="B", message="y")
    out = alerts.list_alerts()
    assert len(out) == 1
    assert out[0]["title"] == "B"
    assert out[0]["severity"] == "error"


def test_clear():
    alerts.set_alert("k", title="t", message="m")
    assert alerts.clear("k") is True
    assert alerts.clear("k") is False
    assert alerts.list_alerts() == []


def test_count():
    alerts.set_alert("a", title="x", message="y")
    alerts.set_alert("b", title="x", message="y")
    assert alerts.count() == 2


def test_health_parses_anonymous():
    class R:
        stdout = "=== Multi Downloader NX ===\nUSER: Anonymous\nYour Country: BR\n"
    with patch("src.health.subprocess.run", return_value=R()), \
         patch("src.health.shutil.which", return_value="/bin/aniDL"):
        r = health.check_crunchyroll()
    assert r["ok"] is False
    assert r["user"] == "Anonymous"


def test_health_parses_authenticated():
    class R:
        stdout = "USER: luisesking (luispaschoal@outlook.com)\nYour Country: BR\n"
    with patch("src.health.subprocess.run", return_value=R()), \
         patch("src.health.shutil.which", return_value="/bin/aniDL"):
        r = health.check_crunchyroll()
    assert r["ok"] is True
    assert "luisesking" in r["user"]
    assert r["error"] is None


def test_health_anidl_missing():
    with patch("src.health.shutil.which", return_value=None):
        r = health.check_crunchyroll()
    assert r["ok"] is False
    assert "PATH" in r["error"]


def test_run_all_checks_clears_alert_when_ok():
    class FakeStore:
        def load(self):
            return {"crunchyroll": {"connected": True}}
    alerts.set_alert("source.crunchyroll.auth", severity="error",
                     title="old", message="old")
    class R:
        stdout = "USER: someone\n"
    with patch("src.health.subprocess.run", return_value=R()), \
         patch("src.health.shutil.which", return_value="/bin/aniDL"):
        health.run_all_checks(FakeStore())
    assert alerts.get("source.crunchyroll.auth") is None


def test_run_all_checks_sets_alert_on_anonymous():
    class FakeStore:
        def load(self):
            return {"crunchyroll": {"connected": True}}
    class R:
        stdout = "USER: Anonymous\n"
    with patch("src.health.subprocess.run", return_value=R()), \
         patch("src.health.shutil.which", return_value="/bin/aniDL"):
        health.run_all_checks(FakeStore())
    a = alerts.get("source.crunchyroll.auth")
    assert a is not None
    assert a.severity == "error"
