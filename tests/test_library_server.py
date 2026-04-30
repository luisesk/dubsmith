"""LibraryServer is a thin wrapper around Plex/Jellyfin HTTP. Test URL/header shape via mocks."""
from unittest.mock import patch

from src.library_server import LibraryServer


class _MockResp:
    def __init__(self, code=200, body=b"", j=None):
        self.status_code = code
        self.content = body
        self._j = j

    def json(self):
        return self._j or {}


def test_returns_false_when_misconfigured():
    ls = LibraryServer("plex", "", "")
    assert ls.refresh_section() is False


def test_plex_refresh_uses_token_param_and_section():
    ls = LibraryServer("plex", "http://plex:32400", "tok", library_section_id=2)
    with patch("src.library_server.httpx.get") as mock_get:
        mock_get.return_value = _MockResp(200)
        ok = ls.refresh_section()
        assert ok is True
        args, kwargs = mock_get.call_args
        assert "/library/sections/2/refresh" in args[0]
        assert kwargs["params"]["X-Plex-Token"] == "tok"


def test_plex_refresh_all_when_no_section():
    ls = LibraryServer("plex", "http://plex:32400", "tok")
    with patch("src.library_server.httpx.get") as mock_get:
        mock_get.return_value = _MockResp(204)
        assert ls.refresh_section() is True
        assert "/library/sections/all/refresh" in mock_get.call_args[0][0]


def test_jellyfin_refresh_uses_emby_token_header():
    ls = LibraryServer("jellyfin", "http://jf:8096", "abc")
    with patch("src.library_server.httpx.post") as mock_post:
        mock_post.return_value = _MockResp(204)
        assert ls.refresh_section() is True
        kwargs = mock_post.call_args.kwargs
        assert kwargs["headers"]["X-Emby-Token"] == "abc"


def test_test_returns_error_dict_on_exception():
    ls = LibraryServer("plex", "http://nope:99", "tok")
    with patch("src.library_server.httpx.get", side_effect=RuntimeError("nope")):
        out = ls.test()
        assert out["ok"] is False
        assert "nope" in out["error"]


def test_jellyfin_test_parses_version():
    ls = LibraryServer("jellyfin", "http://jf:8096", "x")
    with patch("src.library_server.httpx.get") as mock_get:
        mock_get.return_value = _MockResp(200, j={"Version": "10.9.0"})
        out = ls.test()
        assert out["ok"] is True
        assert out["version"] == "10.9.0"
