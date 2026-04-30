"""Audit log: append-only JSONL with structured records."""
import json

from src.audit import AuditLog


def test_write_and_tail(tmp_path):
    a = AuditLog(tmp_path / "audit.log")
    a.write(actor="luis", ip="1.2.3.4", action="login")
    a.write(actor="luis", ip="1.2.3.4", action="user.create", target="alice", role="operator")
    out = a.tail(n=10)
    assert len(out) == 2
    assert out[0]["action"] == "login"
    assert out[1]["target"] == "alice"
    assert out[1]["detail"]["role"] == "operator"


def test_tail_empty_on_missing_file(tmp_path):
    a = AuditLog(tmp_path / "nope.log")
    assert a.tail() == []


def test_failed_action_recorded(tmp_path):
    a = AuditLog(tmp_path / "a.log")
    a.write(actor="x", action="login", ok=False, ip="9.9.9.9")
    out = a.tail()
    assert out[-1]["ok"] is False


def test_skips_corrupt_lines(tmp_path):
    p = tmp_path / "a.log"
    p.write_text('{"ok":true,"action":"x"}\nNOT JSON\n{"ok":false,"action":"y"}\n')
    a = AuditLog(p)
    out = a.tail()
    assert [r["action"] for r in out] == ["x", "y"]


def test_each_line_is_valid_json(tmp_path):
    p = tmp_path / "a.log"
    a = AuditLog(p)
    for i in range(5):
        a.write(actor="u", action="settings.update", target="sonarr", n=i)
    for line in p.read_text().splitlines():
        json.loads(line)  # must not raise
