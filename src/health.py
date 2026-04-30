"""Periodic source health checks. Probes mdnx auth state for each connected source.

When auth is lost, raises an alert via alerts.set_alert("source.<key>.auth", ...).
When recovered, clears it.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess

from . import alerts

log = logging.getLogger(__name__)

USER_RE = re.compile(r"^USER:\s*(.+?)$", re.MULTILINE)


def check_crunchyroll() -> dict:
    """Run a cheap mdnx call (--new) and parse USER: line.

    Returns: {ok: bool, user: str | None, error: str | None}
    """
    if not shutil.which("aniDL"):
        return {"ok": False, "user": None, "error": "aniDL not on PATH"}
    try:
        r = subprocess.run(
            ["aniDL", "--service", "crunchy", "--new"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "user": None, "error": "mdnx timeout"}
    except Exception as e:
        return {"ok": False, "user": None, "error": str(e)[:200]}
    out = r.stdout or ""
    m = USER_RE.search(out)
    if not m:
        return {"ok": False, "user": None, "error": "no USER line in output"}
    u = m.group(1).strip()
    if u.lower().startswith("anonymous"):
        return {"ok": False, "user": "Anonymous", "error": "session expired"}
    return {"ok": True, "user": u, "error": None}


def run_all_checks(sources_store) -> dict:
    """Run all configured-and-connected source checks. Sets/clears alerts."""
    results: dict[str, dict] = {}
    cfg = sources_store.load() if sources_store else {}
    cr = cfg.get("crunchyroll") or {}
    if cr.get("connected"):
        r = check_crunchyroll()
        results["crunchyroll"] = r
        key = "source.crunchyroll.auth"
        if r["ok"]:
            alerts.clear(key)
        else:
            alerts.set_alert(
                key,
                severity="error",
                title="Crunchyroll session expired",
                message=(r.get("error") or "anonymous"),
                actions=[
                    {"label": "Reconnect", "url": "/settings#sources"},
                ],
            )
    # Hidive / ADN: similar pattern when probe added
    return results


def report_episodes_not_selected() -> None:
    """Called by worker when mdnx returns 'Episodes not selected!' — likely auth issue."""
    alerts.set_alert(
        "source.crunchyroll.selection",
        severity="warn",
        title="mdnx couldn't select episode",
        message="Recurring 'Episodes not selected!' usually means a stale CR token. "
                "Reconnect Crunchyroll if the next scheduled health check fails.",
        actions=[{"label": "Settings", "url": "/settings#sources"}],
    )
