"""Security helpers: login throttle, input validators."""
from __future__ import annotations

import re
import time
from collections import deque
from threading import Lock

# Crunchyroll season/show IDs are uppercase alphanumeric, ~10 chars.
# Reject anything else to prevent shell-arg injection (defense in depth — subprocess uses list args).
CR_ID_RE = re.compile(r"^[A-Z0-9_-]{1,32}$")
LANG_CODE_RE = re.compile(r"^[a-zA-Z0-9_-]{1,16}$")

# username: letters, digits, dot, underscore, hyphen
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def valid_cr_id(s: str) -> bool:
    return bool(CR_ID_RE.match(s or ""))


def valid_lang(s: str) -> bool:
    return bool(LANG_CODE_RE.match(s or ""))


def valid_username(s: str) -> bool:
    return bool(USERNAME_RE.match(s or ""))


class LoginThrottle:
    """In-memory sliding-window throttle. Keyed by IP+username."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300, lockout_seconds: int = 900):
        self.max = max_attempts
        self.window = window_seconds
        self.lockout = lockout_seconds
        self._buckets: dict[str, deque] = {}
        self._lock = Lock()

    def _key(self, ip: str, username: str) -> str:
        return f"{ip}|{username.lower()}"

    def is_locked(self, ip: str, username: str) -> tuple[bool, int]:
        """Returns (locked, seconds_left)."""
        now = time.time()
        with self._lock:
            q = self._buckets.get(self._key(ip, username))
            if not q:
                return False, 0
            # drop entries outside window
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) < self.max:
                return False, 0
            oldest = q[0]
            elapsed = now - oldest
            if elapsed >= self.lockout:
                # lockout expired; clear bucket
                self._buckets.pop(self._key(ip, username), None)
                return False, 0
            return True, int(self.lockout - elapsed)

    def record_failure(self, ip: str, username: str) -> None:
        now = time.time()
        with self._lock:
            q = self._buckets.setdefault(self._key(ip, username), deque(maxlen=self.max + 1))
            q.append(now)

    def reset(self, ip: str, username: str) -> None:
        with self._lock:
            self._buckets.pop(self._key(ip, username), None)
