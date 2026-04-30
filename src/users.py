"""Multi-user store with bcrypt-style hashing (uses stdlib hashlib + salt)."""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
from pathlib import Path

import yaml

_LOCK = threading.Lock()

ROLES = ("admin", "operator", "viewer")


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"pbkdf2$200000${salt.hex()}${h.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    try:
        algo, iters, salt_hex, h_hex = hashed.split("$")
        if algo != "pbkdf2":
            return False
        salt = bytes.fromhex(salt_hex)
        h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return secrets.compare_digest(h.hex(), h_hex)
    except Exception:
        return False


class UsersStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save({})

    def load(self) -> dict:
        with _LOCK:
            with open(self.path) as f:
                return yaml.safe_load(f) or {}

    def save(self, users: dict) -> None:
        with _LOCK:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                yaml.safe_dump(users, f, sort_keys=True)
            tmp.replace(self.path)
            os.chmod(self.path, 0o600)

    def get(self, username: str) -> dict | None:
        return self.load().get(username)

    def upsert(self, username: str, password: str | None = None,
               role: str = "operator", **fields) -> dict:
        if role not in ROLES:
            raise ValueError(f"invalid role: {role}")
        users = self.load()
        u = users.get(username) or {}
        if password:
            u["password_hash"] = hash_password(password)
        u["role"] = role
        u.update(fields)
        users[username] = u
        self.save(users)
        return u

    def delete(self, username: str) -> bool:
        users = self.load()
        if username in users:
            del users[username]
            self.save(users)
            return True
        return False

    def verify(self, username: str, password: str) -> bool:
        u = self.get(username)
        if not u or "password_hash" not in u:
            return False
        return verify_password(password, u["password_hash"])

    def list_safe(self) -> list[dict]:
        return [
            {"username": k, "role": v.get("role", "operator")}
            for k, v in self.load().items()
        ]

    def bootstrap(self, default_user: str, default_pass: str) -> None:
        """Create initial admin if no users exist."""
        if not self.load():
            self.upsert(default_user, password=default_pass, role="admin")
