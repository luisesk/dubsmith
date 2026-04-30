"""Append-only audit log for sensitive operations.

Format: one JSON object per line at /data/audit.log. Keys: ts, actor, ip, action, target, ok, detail.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_LOCK = threading.Lock()


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, *, actor: str, action: str, ok: bool = True,
              ip: str = "", target: str = "", **detail) -> None:
        rec = {
            "ts": round(time.time(), 3),
            "actor": actor or "?",
            "ip": ip,
            "action": action,
            "target": target,
            "ok": bool(ok),
            "detail": detail or None,
        }
        try:
            with _LOCK:
                with open(self.path, "a") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("audit write failed: %s", e)

    def tail(self, n: int = 200) -> list[dict]:
        try:
            with _LOCK:
                with open(self.path) as f:
                    lines = f.readlines()[-n:]
        except FileNotFoundError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out
