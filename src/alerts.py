"""In-memory alert store. Each alert has a stable key so we can replace/clear it.

Severity: 'info' | 'warn' | 'error'.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field

_LOCK = threading.Lock()


@dataclass
class Alert:
    key: str
    severity: str           # info | warn | error
    title: str
    message: str
    ts: float = field(default_factory=time.time)
    actions: list[dict] = field(default_factory=list)  # [{label, url}, ...]


_STORE: dict[str, Alert] = {}


def set_alert(key: str, *, severity: str = "warn", title: str,
              message: str, actions: list[dict] | None = None) -> Alert:
    """Add or replace an alert by key."""
    a = Alert(key=key, severity=severity, title=title, message=message,
              actions=actions or [])
    with _LOCK:
        _STORE[key] = a
    return a


def clear(key: str) -> bool:
    with _LOCK:
        return _STORE.pop(key, None) is not None


def list_alerts() -> list[dict]:
    with _LOCK:
        return [asdict(a) for a in sorted(_STORE.values(), key=lambda a: -a.ts)]


def get(key: str) -> Alert | None:
    with _LOCK:
        return _STORE.get(key)


def count() -> int:
    with _LOCK:
        return len(_STORE)
