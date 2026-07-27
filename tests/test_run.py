"""End-to-end pipeline test with stubbed venue + harness (no network, no LLM)."""
from __future__ import annotations

import copy

import pytest

from bot import log, run as run_mod
from bot.models import Book, ForecastResult, Market


def _market(i: int, mid: float = 0.45) -> Market:
    return Market(
        venue="kalshi", market_id=f"KXTEST-26A-T{i}", question=f"Will filing {i} be submitted by Aug 30, 2026?",
        resolution_criteria=("Resolves YES if the SEC EDGAR database shows the Form 10-K "
                             "filed on or before 2026-08-30 23:59 ET per the official record."),
        category="KXTEST", close_time="2026-08-30T23:59:00+00:00",
        resolve_by="2026-08-31T00:00:00+00:00",
        book=Book(bids=[(mid - 0.01, 500)], asks=[(mid + 0.01, 500)]),
        fee_params={"model": "kalshi_quadratic", "taker_rate": 0.07, "maker_rate": 0.0},
        raw={"event_ticker": f"KXTEST-26A"},
    )


@pytest.fixture
def cfg(tmp_path):
    c = run_mod.load_config()
    c = copy.deepcopy(c)
    c["run"]["db_path"] = str(tmp_path / "t.db")
    c["run"]["max_run_cost_usd"] = 10.0
    return c


@pytest.fixture
def stubs(monkeypatch):
    markets = [_market(i) for i in range(4)]

    monkeypatch.setattr(run_mod.connectors, "list_markets",
                        lambda venue, limit=None: markets[:limit] if limit else markets)
    monkeypatch.setattr(run_mod.connectors, "get_book",
                        lambda m: Book(bids=[(0.44, 500)], asks=[(0.46, 500)]))
    monkeypatch.setattr(run_mod.settle, "settle_open",
                        lambda con, cfg: {"checked": 0, "settled": 0,
                                          "disputed": 0, "overdue": []})
    monkeypatch.setattr(
        run_mod.forecast_mod, "forecast_sync",
        lambda m, cfg: ForecastResult(p=0.62, rationale="stub", sources=["sec.gov"],
                                      criteria_interp="clear", ensemble_members=[0.6, 0.62, 0.64],
                                      ensemble_spread=0.04, cost_usd=1.0))

    async def fake_nr(m, cfg):
        return 0.55, 0.05
    monkeypatch.setattr(run_mod.baselines, "no_research_baseline", fake_nr)
    return markets


def test_full_pipeline(cfg, stubs):
    con = log.connect(cfg["run"]["db_path"])
    summary = run_mod.run_pipeline(cfg, fast=True, con=con)
    assert summary["status"] == "ok"
    assert summary["n_forecast"] > 0
    rows = con.execute("SELECT * FROM forecasts").fetchall()
    assert len(rows) == summary["n_forecast"]
    r = rows[0]
    assert r["p"] == 0.62 and r["baseline_norsrch_p"] == 0.55
    assert r["cluster_id"] and r["harness_version"] == cfg["harness_version"]
    assert r["q_mid_decide"] == pytest.approx(0.45)
    # p=0.62 vs mid 0.45 clears the taker threshold: expect action, executable fill
    traded = [x for x in rows if x["side"] != "pass"]
    assert traded and all(x["fill_price"] is not None or x["order_type"] == "maker"
                          for x in traded)
    runs = con.execute("SELECT * FROM runs").fetchall()
    assert runs[-1]["status"] == "ok" and runs[-1]["funnel"]


def test_dedupe_and_baseline_mode(cfg, stubs):
    con = log.connect(cfg["run"]["db_path"])
    s1 = run_mod.run_pipeline(cfg, fast=True, con=con)
    s2 = run_mod.run_pipeline(cfg, fast=True, con=con)  # same markets, same version
    assert s2["n_forecast"] == 0  # already_forecast dedupe
    s3 = run_mod.run_pipeline(cfg, fast=True, baseline_only=True, con=con)
    assert s3["n_forecast"] > 0 and s3["cost_usd"] == 0
    row = con.execute("SELECT * FROM forecasts WHERE harness_version='p0-baseline'").fetchone()
    assert row["side"] == "pass" and row["p"] == row["baseline_market_p"]


def test_budget_halt(cfg, stubs):
    cfg["run"]["max_run_cost_usd"] = 1.0  # first market costs 1.05
    con = log.connect(cfg["run"]["db_path"])
    summary = run_mod.run_pipeline(cfg, fast=True, con=con)
    assert summary["status"] == "budget-halt"
    assert summary["n_forecast"] < 4
    assert con.execute("SELECT status FROM runs").fetchone()["status"] == "error"
