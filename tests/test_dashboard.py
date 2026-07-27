"""Pure-helper tests for dashboard/app.py.

No `streamlit run`, no evaluate dependency: everything here exercises the
pure helpers plus the import-with-empty-DB contract (a fresh clone must not
crash on `import dashboard.app`).
"""
from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def app(tmp_path_factory):
    """Import dashboard.app with BOT_DB_PATH pointing at a tmp empty DB."""
    db = tmp_path_factory.mktemp("db") / "empty.db"
    os.environ["BOT_DB_PATH"] = str(db)
    sys.path.insert(0, str(ROOT))
    mod = importlib.import_module("dashboard.app")
    return mod


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


# --- import / empty-DB contract --------------------------------------------

def test_import_does_not_crash_and_does_not_need_runtime(app):
    # Import happened in the fixture without a streamlit runtime; main exists
    # but was not invoked (no exception surfaced during import).
    assert callable(app.main)
    assert os.environ["BOT_DB_PATH"] == app.resolve_db_path()


def test_loaders_on_empty_db(app, tmp_path):
    from bot import log as botlog
    con = botlog.connect(tmp_path / "fresh.db")
    try:
        df = app.load_forecasts(con)
        runs = app.load_runs(con)
    finally:
        con.close()
    assert df.empty and "outcome" in df.columns
    assert runs.empty and "funnel" in runs.columns
    # Downstream pure helpers tolerate the empty frames.
    assert app.headline_deltas(df).empty
    assert app.running_cs(df, rho=0.1).empty  # returns before importing evaluate
    assert app.latest_funnel(runs) == []
    assert app.paper_pnl(df) == 0.0


# --- overdue_ids ------------------------------------------------------------

def test_overdue_ids(app):
    rows = [
        {"id": 1, "resolve_by": (NOW - timedelta(days=5)).isoformat()},   # overdue
        {"id": 2, "resolve_by": (NOW - timedelta(days=1)).isoformat()},   # in grace
        {"id": 3, "resolve_by": (NOW + timedelta(days=2)).isoformat()},   # future
        {"id": 4, "resolve_by": None},                                    # skipped
        {"id": 5, "resolve_by": "not-a-date"},                            # skipped
        {"id": 6, "resolve_by": "2026-07-20T00:00:00Z"},                  # Z suffix, overdue
    ]
    assert app.overdue_ids(rows, NOW) == [1, 6]


def test_overdue_ids_accepts_dataframe_and_exact_boundary(app):
    df = pd.DataFrame([
        {"id": 10, "resolve_by": (NOW - timedelta(days=3)).isoformat()},  # exactly 3d: NOT overdue
        {"id": 11, "resolve_by": (NOW - timedelta(days=3, seconds=1)).isoformat()},
    ])
    assert app.overdue_ids(df, NOW) == [11]
    assert app.overdue_ids([], NOW) == []


# --- significance_line ------------------------------------------------------

def test_significance_line_none_fields(app):
    line = app.significance_line({}, 400)
    assert "not yet significant" in line
    assert "0 resolved" in line
    assert "N* = 400" in line
    # None-valued fields (tiny-N contract) behave like missing ones.
    line2 = app.significance_line(
        {"n": None, "n_eff": None, "delta_log": None,
         "ci_lo": None, "ci_hi": None}, 400)
    assert "not yet significant" in line2
    assert app.significance_line(None, 400)  # even a None dict works


def test_significance_line_significant(app):
    line = app.significance_line(
        {"n": 120, "n_eff": 45, "delta_log": 0.06,
         "ci_lo": 0.01, "ci_hi": 0.11}, 400)
    assert "currently significant" in line
    assert "not yet" not in line
    assert "120 resolved" in line and "45 clusters" in line
    assert "0.0600" in line and "[0.0100, 0.1100]" in line


def test_significance_line_clearly_negative(app):
    line = app.significance_line(
        {"n": 80, "n_clusters": 30, "delta_log": -0.09,
         "ci_lo": -0.15, "ci_hi": -0.02}, 400)
    assert "clearly negative" in line
    assert "30 clusters" in line  # n_clusters fallback when n_eff missing


# --- funnel helper ----------------------------------------------------------

def test_latest_funnel_picks_newest_run_with_data(app):
    runs = pd.DataFrame([  # newest first, as load_runs returns
        {"id": 3, "funnel": None},                                   # no data
        {"id": 2, "funnel": '{"ingested": 5000, "vertical": 300, '
                            '"clarity": 60, "forecasted": 20}'},
        {"id": 1, "funnel": '{"ingested": 9}'},                      # older
    ])
    assert app.latest_funnel(runs) == [
        ("ingested", 5000), ("vertical", 300), ("clarity", 60),
        ("forecasted", 20)]


def test_latest_funnel_handles_dicts_and_garbage(app):
    runs = [
        {"id": 4, "funnel": "not json"},
        {"id": 3, "funnel": float("nan")},
        {"id": 2, "funnel": {"a": 1, "b": 2}},  # already-parsed dict
    ]
    assert app.latest_funnel(runs) == [("a", 1), ("b", 2)]
    assert app.latest_funnel([]) == []
    assert app.latest_funnel(None) == []


# --- paper P&L helper -------------------------------------------------------

def test_paper_pnl(app):
    rows = [
        # yes side, won: (1 - 0.6 - 0.01) * 10 * 1.0 = 3.9
        {"side": "yes", "outcome": 1, "fill_price": 0.6, "stake": 10,
         "fill_qty_frac": 1.0, "fee": 0.01},
        # no side, outcome 0 => payout 1: (1 - 0.3) * 10 * 0.5 = 3.5
        {"side": "no", "outcome": 0, "fill_price": 0.3, "stake": 10,
         "fill_qty_frac": 0.5, "fee": 0.0},
        # yes side, lost: (0 - 0.4) * 5 * 1.0 = -2.0
        {"side": "yes", "outcome": 0, "fill_price": 0.4, "stake": 5,
         "fill_qty_frac": 1.0},
        # ignored: pass, unresolved, unfilled
        {"side": "pass", "outcome": 1, "fill_price": 0.5, "stake": 100,
         "fill_qty_frac": 1.0},
        {"side": "yes", "outcome": None, "fill_price": 0.5, "stake": 100,
         "fill_qty_frac": 1.0},
        {"side": "yes", "outcome": 1, "fill_price": None, "stake": 100,
         "fill_qty_frac": 1.0},
        {"side": "yes", "outcome": 1, "fill_price": 0.5, "stake": 100,
         "fill_qty_frac": None},  # frac None -> 0 contribution
    ]
    assert app.paper_pnl(rows) == pytest.approx(3.9 + 3.5 - 2.0)
    assert app.paper_pnl(pd.DataFrame(rows)) == pytest.approx(3.9 + 3.5 - 2.0)
    assert app.paper_pnl([]) == 0.0


# --- headline_deltas (feeds the CS) ----------------------------------------

def test_headline_deltas_filters_and_fallback(app):
    df = pd.DataFrame([
        # resolved, clean, baseline present -> kept with q=baseline
        {"id": 1, "ts_forecast": "2026-07-01T00:00:00+00:00", "p": 0.7,
         "baseline_market_p": 0.5, "q_mid_snap": 0.55, "outcome": 1,
         "contaminated": 0},
        # baseline missing -> falls back to q_mid_snap
        {"id": 2, "ts_forecast": "2026-07-02T00:00:00+00:00", "p": 0.4,
         "baseline_market_p": None, "q_mid_snap": 0.45, "outcome": 0,
         "contaminated": None},
        # contaminated -> excluded from the headline
        {"id": 3, "ts_forecast": "2026-07-03T00:00:00+00:00", "p": 0.9,
         "baseline_market_p": 0.5, "q_mid_snap": 0.5, "outcome": 1,
         "contaminated": 1},
        # unresolved -> excluded
        {"id": 4, "ts_forecast": "2026-07-04T00:00:00+00:00", "p": 0.6,
         "baseline_market_p": 0.5, "q_mid_snap": 0.5, "outcome": None,
         "contaminated": 0},
        # no q anywhere -> excluded
        {"id": 5, "ts_forecast": "2026-07-05T00:00:00+00:00", "p": 0.6,
         "baseline_market_p": None, "q_mid_snap": None, "outcome": 1,
         "contaminated": 0},
    ])
    out = app.headline_deltas(df)
    assert list(out["p"]) == [0.7, 0.4]
    assert list(out["q"]) == [0.5, 0.45]
    assert list(out["o"]) == [1.0, 0.0]


# --- local scoring helpers used by panel 2 ----------------------------------

def test_local_scores_and_calibration(app):
    p = [0.8, 0.8, 0.2, 0.2]
    o = [1, 1, 0, 0]
    assert app.brier(p, o) == pytest.approx(0.04)
    assert app.log_score(p, o) < 0
    cal = app.calibration_table(p, o, bins=10)
    assert set(cal.columns) == {"mean_pred", "obs_freq", "n", "se"}
    assert cal["n"].sum() == 4
    # bins: p=0.2 -> obs 0.0, p=0.8 -> obs 1.0; pure 0/1 bins have se 0
    assert sorted(cal["obs_freq"]) == [0.0, 1.0]
    assert (cal["se"] == 0.0).all()
    mur = app.murphy_decomposition(p, o)
    assert mur["uncertainty"] == pytest.approx(0.25)
    assert mur["reliability"] == pytest.approx(0.04)
    assert mur["resolution"] == pytest.approx(0.25)
    # decomposition identity: BS = rel - res + unc
    assert app.brier(p, o) == pytest.approx(
        mur["reliability"] - mur["resolution"] + mur["uncertainty"])
    # empty inputs degrade to None, not crash
    assert app.murphy_decomposition([], [])["uncertainty"] is None
    assert app.calibration_table([], []).empty
