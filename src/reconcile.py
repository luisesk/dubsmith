"""Reconciliation: keep Dubsmith state in sync with Sonarr.

When a series is deleted in Sonarr, its sid must also be removed from
shows.yml and any pending/running queue jobs for it must be cleared.
Done jobs are kept for history.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def run(sonarr, shows_store, queue) -> dict:
    """Walk Sonarr, find tracked sids that no longer exist there, drop them.
    Returns: {checked, removed: [{series_id, name}], queue_cleared}.
    """
    out = {"checked": 0, "removed": [], "queue_cleared": 0, "error": None}
    try:
        sonarr_ids = {int(s["id"]) for s in sonarr.all_series()}
    except Exception as e:
        out["error"] = f"sonarr unreachable: {e}"
        log.warning("reconcile: sonarr.all_series failed: %s", e)
        return out

    tracked = shows_store.load() or {}
    out["checked"] = len(tracked)
    for k, sh in list(tracked.items()):
        try:
            sid = int(k)
        except (TypeError, ValueError):
            continue
        if sid in sonarr_ids:
            continue
        # Series gone from Sonarr — drop from Dubsmith.
        name = (sh or {}).get("name", str(sid))
        shows_store.delete(sid)
        # Clear pending/non-terminal jobs; keep done/quarantined for history.
        n = 0
        for state in ("pending", "downloading", "syncing", "muxing", "failed"):
            n += queue.delete_where(state=state) if False else 0  # noqa
        # Targeted: only this series' non-done jobs. queue.delete_where lacks
        # series-scoping, so do it inline via a one-off connection.
        n = _delete_series_pending(queue, sid)
        out["removed"].append({"series_id": sid, "name": name, "queue_cleared": n})
        out["queue_cleared"] += n
        log.info("reconcile: removed sid=%d name=%r (cleared %d queue jobs)",
                 sid, name, n)
    return out


def _delete_series_pending(queue, series_id: int) -> int:
    """Remove non-done queue jobs for a given series."""
    import sqlite3
    with sqlite3.connect(queue.db_path, isolation_level=None, timeout=30) as c:
        cur = c.execute(
            "DELETE FROM jobs WHERE series_id=? AND state IN "
            "('pending','downloading','syncing','muxing','failed')",
            (series_id,),
        )
        return cur.rowcount
