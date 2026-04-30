"""Tests for input validators + login throttle."""
import time

from src.security import LoginThrottle, valid_cr_id, valid_lang, valid_username


def test_valid_cr_id():
    assert valid_cr_id("GR49C7EPD")
    assert valid_cr_id("ABC_123-XYZ")
    assert not valid_cr_id("")
    assert not valid_cr_id("a; rm -rf /")
    assert not valid_cr_id("with space")
    assert not valid_cr_id("X" * 33)


def test_valid_lang():
    assert valid_lang("pt-BR")
    assert valid_lang("eng")
    assert not valid_lang("../etc")
    assert not valid_lang("$PATH")


def test_valid_username():
    assert valid_username("luis")
    assert valid_username("user.name_2")
    assert not valid_username("")
    assert not valid_username("user@host")
    assert not valid_username("a" * 65)


def test_throttle_locks_after_max():
    t = LoginThrottle(max_attempts=3, window_seconds=60, lockout_seconds=60)
    locked, _ = t.is_locked("1.2.3.4", "u")
    assert not locked
    for _ in range(3):
        t.record_failure("1.2.3.4", "u")
    locked, secs = t.is_locked("1.2.3.4", "u")
    assert locked
    assert 0 < secs <= 60


def test_throttle_reset_clears_bucket():
    t = LoginThrottle(max_attempts=2, window_seconds=60, lockout_seconds=60)
    t.record_failure("ip", "user")
    t.record_failure("ip", "user")
    assert t.is_locked("ip", "user")[0]
    t.reset("ip", "user")
    assert not t.is_locked("ip", "user")[0]


def test_throttle_isolated_per_user():
    t = LoginThrottle(max_attempts=2, window_seconds=60, lockout_seconds=60)
    for _ in range(2):
        t.record_failure("ip", "alice")
    assert t.is_locked("ip", "alice")[0]
    assert not t.is_locked("ip", "bob")[0]


def test_throttle_window_expires():
    t = LoginThrottle(max_attempts=2, window_seconds=1, lockout_seconds=1)
    t.record_failure("ip", "u")
    t.record_failure("ip", "u")
    assert t.is_locked("ip", "u")[0]
    time.sleep(1.2)
    # lockout expired; bucket cleared on next check
    assert not t.is_locked("ip", "u")[0]
