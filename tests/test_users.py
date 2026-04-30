"""Tests for UsersStore + password hashing."""
import pytest

from src.users import UsersStore, hash_password, verify_password


def test_hash_verify_roundtrip():
    h = hash_password("hunter2")
    assert verify_password("hunter2", h)
    assert not verify_password("hunter3", h)


def test_hash_unique_salts():
    a = hash_password("same")
    b = hash_password("same")
    assert a != b
    assert verify_password("same", a)
    assert verify_password("same", b)


def test_verify_rejects_garbage():
    assert not verify_password("x", "")
    assert not verify_password("x", "not-a-hash")
    assert not verify_password("x", "md5$1$ab$cd")


def test_users_bootstrap_then_no_op(tmp_path):
    s = UsersStore(tmp_path / "u.yml")
    s.bootstrap("admin", "secret")
    assert s.verify("admin", "secret")
    # second call must be no-op
    s.bootstrap("admin", "newpass")
    assert s.verify("admin", "secret")


def test_users_upsert_role_validation(tmp_path):
    s = UsersStore(tmp_path / "u.yml")
    s.upsert("alice", password="p", role="operator")
    with pytest.raises(ValueError):
        s.upsert("bob", password="p", role="god")


def test_users_change_password(tmp_path):
    s = UsersStore(tmp_path / "u.yml")
    s.upsert("a", password="old", role="admin")
    assert s.verify("a", "old")
    s.upsert("a", password="new", role="admin")
    assert not s.verify("a", "old")
    assert s.verify("a", "new")


def test_users_list_safe_omits_hash(tmp_path):
    s = UsersStore(tmp_path / "u.yml")
    s.upsert("a", password="p", role="viewer")
    out = s.list_safe()
    assert out == [{"username": "a", "role": "viewer"}]


def test_users_delete(tmp_path):
    s = UsersStore(tmp_path / "u.yml")
    s.upsert("a", password="p", role="viewer")
    assert s.delete("a") is True
    assert s.delete("a") is False
    assert s.verify("a", "p") is False
