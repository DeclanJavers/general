"""Append-only forecast log over SQLite — the scientific artifact.

Immutability contract: forecast-time fields are written once by
insert_forecast() and never modified. The ONLY later writes are:
  - settle():        resolved_at, outcome, disputed, resolution_note
  - set_markouts():  markout_5m, markout_1h (fill-quality measurement)
Everything else (dashboard, evaluator) is read-only.

The runs table records pipeline run health for the ops panel.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "forecasts.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
  id                 INTEGER PRIMARY KEY,
  ts_forecast        TEXT NOT NULL,
  venue              TEXT NOT NULL,
  market_id          TEXT NOT NULL,
  question           TEXT,
  category           TEXT,
  vertical           TEXT,
  cluster_id         TEXT NOT NULL,
  p                  REAL NOT NULL,
  q_mid_snap         REAL,
  bid_snap           REAL,
  ask_snap           REAL,
  q_mid_decide       REAL,
  bid_decide         REAL,
  ask_decide         REAL,
  edge               REAL,
  side               TEXT NOT NULL,
  order_type         TEXT,
  order_price        REAL,
  stake              REAL,
  fill_price         REAL,
  fill_qty_frac      REAL,
  fee                REAL,
  fee_params         TEXT,
  harness_version    TEXT NOT NULL,
  decision_version   TEXT NOT NULL,
  rationale          TEXT,
  sources            TEXT,
  criteria_interp    TEXT,
  ensemble_members   TEXT,
  ensemble_spread    REAL,
  retrieval_ok       INTEGER,
  contaminated       INTEGER,
  baseline_market_p  REAL,
  baseline_norsrch_p REAL,
  cost_usd           REAL,
  resolve_by         TEXT,
  markout_5m         REAL,
  markout_1h         REAL,
  resolved_at        TEXT,
  outcome            INTEGER,
  disputed           INTEGER,
  resolution_note    TEXT
);
CREATE INDEX IF NOT EXISTS idx_forecasts_open
  ON forecasts (venue, market_id) WHERE outcome IS NULL;

CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY,
  ts_start    TEXT NOT NULL,
  ts_end      TEXT,
  status      TEXT NOT NULL,        -- 'running' | 'ok' | 'error'
  stage       TEXT,                 -- last stage reached
  n_ingested  INTEGER, n_selected INTEGER, n_forecast INTEGER, n_traded INTEGER,
  cost_usd    REAL,
  error       TEXT,
  funnel      TEXT                  -- JSON: per-gate survivor counts
);
"""

# Fields settlement/markouts may write; everything else is insert-only.
_SETTLE_FIELDS = {"resolved_at", "outcome", "disputed", "resolution_note"}
_MARKOUT_FIELDS = {"markout_5m", "markout_1h"}
_JSON_FIELDS = {"sources", "ensemble_members", "fee_params", "funnel"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path = DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the log database."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def insert_forecast(con: sqlite3.Connection, **fields) -> int:
    """Append one forecast row. Forecast-time fields only; never updates."""
    forbidden = (_SETTLE_FIELDS | _MARKOUT_FIELDS) & fields.keys()
    if forbidden:
        raise ValueError(f"settlement/markout fields not allowed at insert: {forbidden}")
    fields.setdefault("ts_forecast", now_iso())
    for k in _JSON_FIELDS & fields.keys():
        if not isinstance(fields[k], (str, type(None))):
            fields[k] = json.dumps(fields[k])
    cols = ", ".join(fields)
    ph = ", ".join("?" for _ in fields)
    cur = con.execute(f"INSERT INTO forecasts ({cols}) VALUES ({ph})",
                      list(fields.values()))
    con.commit()
    return cur.lastrowid


def settle(con: sqlite3.Connection, row_id: int, outcome: int,
           disputed: bool = False, resolution_note: str = "",
           resolved_at: str | None = None) -> None:
    """The only permitted post-insert write besides markouts."""
    if outcome not in (0, 1):
        raise ValueError("outcome must be 0 or 1")
    con.execute(
        "UPDATE forecasts SET resolved_at=?, outcome=?, disputed=?, resolution_note=?"
        " WHERE id=? AND outcome IS NULL",
        (resolved_at or now_iso(), outcome, int(disputed), resolution_note, row_id))
    con.commit()


def set_markouts(con: sqlite3.Connection, row_id: int,
                 markout_5m: float | None = None,
                 markout_1h: float | None = None) -> None:
    sets, vals = [], []
    if markout_5m is not None:
        sets.append("markout_5m=?"); vals.append(markout_5m)
    if markout_1h is not None:
        sets.append("markout_1h=?"); vals.append(markout_1h)
    if sets:
        con.execute(f"UPDATE forecasts SET {', '.join(sets)} WHERE id=?",
                    (*vals, row_id))
        con.commit()


def open_rows(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Forecasts awaiting resolution."""
    return con.execute(
        "SELECT * FROM forecasts WHERE outcome IS NULL ORDER BY ts_forecast").fetchall()


def resolved_rows(con: sqlite3.Connection,
                  harness_version: str | None = None,
                  include_disputed: bool = True) -> list[sqlite3.Row]:
    q = "SELECT * FROM forecasts WHERE outcome IS NOT NULL"
    args: list = []
    if harness_version:
        q += " AND harness_version=?"; args.append(harness_version)
    if not include_disputed:
        q += " AND (disputed IS NULL OR disputed=0)"
    return con.execute(q + " ORDER BY ts_forecast", args).fetchall()


def already_forecast(con: sqlite3.Connection, venue: str, market_id: str,
                     harness_version: str) -> bool:
    """One live forecast per market per harness version."""
    return con.execute(
        "SELECT 1 FROM forecasts WHERE venue=? AND market_id=? AND harness_version=?",
        (venue, market_id, harness_version)).fetchone() is not None


def start_run(con: sqlite3.Connection) -> int:
    cur = con.execute("INSERT INTO runs (ts_start, status) VALUES (?, 'running')",
                      (now_iso(),))
    con.commit()
    return cur.lastrowid


def finish_run(con: sqlite3.Connection, run_id: int, status: str, **fields) -> None:
    for k in _JSON_FIELDS & fields.keys():
        if not isinstance(fields[k], (str, type(None))):
            fields[k] = json.dumps(fields[k])
    sets = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE runs SET ts_end=?, status=?{', ' + sets if sets else ''} WHERE id=?",
                (now_iso(), status, *fields.values(), run_id))
    con.commit()
