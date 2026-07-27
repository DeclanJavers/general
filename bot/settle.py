"""Settlement tracker — closes the loop (doc §2.7).

Polls each open forecast's market for resolution and records the outcome via
log.settle() (the only permitted post-insert write). Disputed resolutions are
never silently dropped: they are written with disputed=1 plus the note so the
evaluator can report headline numbers with and without them.

Expected connector contract — connectors.get_resolution(venue, market_id)
returns None while unresolved, else a dict:
  {"outcome": 0|1, "disputed": bool, "resolution_note"/"note": str,
   "resolved_at": iso-ts (optional)}
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from bot import log

OVERDUE_DAYS = 3  # flag open rows whose resolve_by is this many days past


def settle_open(con: sqlite3.Connection, cfg: dict) -> dict:
    """Settle every open forecast that has resolved; summarize for the ops
    panel: {"checked": n, "settled": n, "disputed": n, "overdue": [row ids]}.

    Overdue = still-open rows > OVERDUE_DAYS past resolve_by (summary-only;
    the DB is never touched for them). Rows that settle on this pass are no
    longer flagged — the queue shows what still needs attention.
    """
    from bot import connectors  # lazy: no network dep at import; tests patch

    overdue_days = (cfg or {}).get("settlement", {}).get(
        "overdue_days", OVERDUE_DAYS)
    now = datetime.now(timezone.utc)
    summary: dict = {"checked": 0, "settled": 0, "disputed": 0, "overdue": []}
    for row in log.open_rows(con):
        summary["checked"] += 1
        res = connectors.get_resolution(row["venue"], row["market_id"])
        if res is not None and res.get("outcome") is not None:
            disputed = bool(res.get("disputed", False))
            note = res.get("resolution_note") or res.get("note") or ""
            log.settle(con, row["id"], int(res["outcome"]), disputed=disputed,
                       resolution_note=note, resolved_at=res.get("resolved_at"))
            summary["settled"] += 1
            if disputed:
                summary["disputed"] += 1
        elif _overdue(row["resolve_by"], now, overdue_days):
            summary["overdue"].append(row["id"])
    return summary


def _overdue(resolve_by: str | None, now: datetime, days: float) -> bool:
    if not resolve_by:
        return False
    try:
        dt = datetime.fromisoformat(resolve_by.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return now - dt > timedelta(days=days)
