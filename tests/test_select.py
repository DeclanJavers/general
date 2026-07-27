"""Tests for the selection funnel (bot/select.py) and settlement tracker
(bot/settle.py). All markets are built in-code; no network."""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from bot import log, select, settle
from bot.models import Book, Market

NOW = datetime.now(timezone.utc)
CFG = {
    "categories_exclude": ["fed", "cpi", "elections_top", "sports_main",
                           "crypto_price", "mve"],
    "max_spread": 0.05,
    "min_book_sides": 2,
    "max_days_to_resolution": 90,
    "min_days_to_resolution": 0,
    "theta_hurdle_annual": 0.05,
    "shortlist_size": 25,
}

GOOD_CRITERIA = ("Resolves YES if the bill is signed into law as published "
                 "in the Federal Register on congress.gov by 2026-09-30.")


def iso(days: float) -> str:
    return (NOW + timedelta(days=days)).isoformat(timespec="seconds")


def good_book() -> Book:
    return Book(bids=[(0.40, 100.0)], asks=[(0.43, 120.0)], ts=iso(0))


_DEFAULT = object()


def mk(question="Will the FCC bill be signed into law?", venue="kalshi",
       market_id="KXBILL-25XYZ-T3", criteria=GOOD_CRITERIA, category="politics",
       resolve_by=iso(30), close_time=None, book=_DEFAULT, **kw) -> Market:
    return Market(venue=venue, market_id=market_id, question=question,
                  resolution_criteria=criteria, category=category,
                  resolve_by=resolve_by, close_time=close_time,
                  book=good_book() if book is _DEFAULT else book, **kw)


# --- vertical gate -----------------------------------------------------------

def test_vertical_keeps_procedural():
    assert select.vertical_ok(mk(), CFG["categories_exclude"])


@pytest.mark.parametrize("question,category", [
    ("Will the Fed cut interest rates in September?", "economics"),
    ("Will CPI come in above expectations?", "economics"),
    ("Will the S&P close above its record?", "finance"),
    ("Will Bitcoin price hit a new high?", "finance"),
    ("Who will win the presidential election?", "politics"),
    ("Will the Chiefs win the Super Bowl?", "entertainment"),
    ("Will it rain tomorrow?", "elections"),        # category-string match
    ("Will it rain tomorrow?", "crypto markets"),   # category-string match
])
def test_vertical_drops_excluded(question, category):
    m = mk(question=question, category=category)
    assert not select.vertical_ok(m, CFG["categories_exclude"])


def test_prefer_bonus():
    assert select.prefer_bonus(mk(question="Will the FDA approve the filing?")) == 1
    assert select.prefer_bonus(mk(question="Will it snow in Boston?")) == 0


# --- clarity gate ------------------------------------------------------------

def test_clarity_keeps_sourced_dated_criteria():
    assert select.clarity_ok(mk())
    assert select.clarity_score(mk()) == 2


@pytest.mark.parametrize("criteria", [
    "",                                        # empty
    "Resolves YES if it happens.",             # too short
    ("Resolves YES if the change is substantially complete in the opinion of "
     "reviewers and widely reported by consensus of media outlets."),  # flags
])
def test_clarity_drops_bad_criteria(criteria):
    assert not select.clarity_ok(mk(criteria=criteria))


def test_clarity_polymarket_higher_bar():
    # Source but no explicit date => score 1: passes kalshi, fails polymarket.
    criteria = ("Resolves YES per the official announcement text published "
                "on the agency website, according to the press release.")
    assert select.clarity_ok(mk(criteria=criteria, venue="kalshi"))
    assert not select.clarity_ok(mk(criteria=criteria, venue="polymarket"))
    assert select.clarity_ok(mk(venue="polymarket"))  # score 2 clears the bar


# --- time gate ---------------------------------------------------------------

def test_time_gate():
    assert select.time_ok(mk(resolve_by=iso(30)), CFG, NOW)
    assert not select.time_ok(mk(resolve_by=iso(120)), CFG, NOW)  # too far
    assert not select.time_ok(mk(resolve_by=iso(-2)), CFG, NOW)   # past
    # close_time fallback; missing both => drop.
    assert select.time_ok(mk(resolve_by=None, close_time=iso(10)), CFG, NOW)
    assert not select.time_ok(mk(resolve_by=None, close_time=None), CFG, NOW)


# --- liquidity gate ----------------------------------------------------------

def test_liquidity_gate():
    assert select.liquidity_ok(mk(), CFG)
    one_sided = Book(bids=[(0.40, 100.0)], asks=[])
    assert not select.liquidity_ok(mk(book=one_sided), CFG)
    wide = Book(bids=[(0.30, 100.0)], asks=[(0.45, 100.0)])
    assert not select.liquidity_ok(mk(book=wide), CFG)
    empty_depth = Book(bids=[(0.40, 0.0)], asks=[(0.43, 100.0)])
    assert not select.liquidity_ok(mk(book=empty_depth), CFG)
    assert not select.liquidity_ok(mk(book=None), CFG)


# --- theta gate --------------------------------------------------------------

def test_theta_annotates_and_rarely_drops():
    m = mk(resolve_by=iso(73))  # 0.05 * 73/365 = 0.01
    assert select.theta_ok(m, CFG, NOW)
    assert m.raw["theta_hurdle"] == pytest.approx(0.01, abs=1e-4)
    hot = mk(resolve_by=iso(80))
    cfg = dict(CFG, theta_hurdle_annual=0.5)  # 0.5 * 80/365 = 0.11 > 0.10
    assert not select.theta_ok(hot, cfg, NOW)
    assert hot.raw["theta_hurdle"] > select.THETA_DROP


# --- funnel ------------------------------------------------------------------

def test_funnel_counts_monotonic_and_clusters_assigned():
    markets = [
        mk(market_id="KXBILL-25XYZ-T3"),
        mk(market_id="KXBILL-25XYZ-T5"),
        mk(question="Will the Fed cut interest rates?"),          # vertical
        mk(criteria="Too short."),                                # clarity
        mk(resolve_by=iso(200)),                                  # time
        mk(book=Book(bids=[(0.4, 50.0)], asks=[])),               # liquidity
    ]
    shortlist, counts = select.run_funnel(markets, CFG, now=NOW)
    order = ["ingested", "vertical", "clarity", "time", "liquidity", "theta",
             "shortlist"]
    assert list(counts) == order
    vals = [counts[k] for k in order]
    assert vals[0] == 6
    assert all(a >= b for a, b in zip(vals, vals[1:]))
    assert counts["shortlist"] == len(shortlist) == 2
    assert all(m.cluster_id for m in shortlist)


def test_funnel_shortlist_size_cap():
    markets = [mk(market_id=f"KXBILL-25A{i:02d}-T1") for i in range(5)]
    shortlist, counts = select.run_funnel(markets, dict(CFG, shortlist_size=3),
                                          now=NOW)
    assert counts["shortlist"] == len(shortlist) == 3


# --- clustering --------------------------------------------------------------

def test_cluster_kalshi_same_event_groups():
    a = mk(market_id="KXBILL-25XYZ-T3")
    b = mk(market_id="KXBILL-25XYZ-T5")
    c = mk(market_id="KXVETO-26ABC-T1")
    assert select.assign_cluster(a) == select.assign_cluster(b) == \
        "kalshi:KXBILL-25XYZ"
    assert select.assign_cluster(c) != select.assign_cluster(a)


def test_cluster_kalshi_prefers_raw_event_ticker():
    m = mk(market_id="KXBILL-25XYZ-T3", raw={"event_ticker": "KXBILL-25XYZ"})
    assert select.assign_cluster(m) == "kalshi:KXBILL-25XYZ"


def test_cluster_manifold_slug_strips_dates_and_numbers():
    a = mk(venue="manifold", market_id="m1",
           question="Will OpenAI release GPT-6 by March 2026?")
    b = mk(venue="manifold", market_id="m2",
           question="Will OpenAI release GPT-6 by June 2026?")
    c = mk(venue="manifold", market_id="m3",
           question="Will NASA launch Artemis III by June 2026?")
    assert select.assign_cluster(a) == select.assign_cluster(b)
    assert select.assign_cluster(a).startswith("manifold:")
    assert select.assign_cluster(c) != select.assign_cluster(a)


# --- settlement --------------------------------------------------------------

def _insert(con, market_id, resolve_by):
    return log.insert_forecast(
        con, venue="kalshi", market_id=market_id, question="q?",
        cluster_id=f"kalshi:{market_id}", p=0.6, side="yes",
        harness_version="h1.0.0", decision_version="d1.0.0",
        resolve_by=resolve_by)


def test_settle_open(tmp_path, monkeypatch):
    con = log.connect(tmp_path / "forecasts.db")
    r1 = _insert(con, "KX-A-T1", iso(-1))   # resolves clean
    r2 = _insert(con, "KX-B-T1", iso(-2))   # resolves disputed
    r3 = _insert(con, "KX-C-T1", iso(-5))   # unresolved, overdue
    r4 = _insert(con, "KX-D-T1", iso(10))   # unresolved, not due yet

    resolutions = {
        "KX-A-T1": {"outcome": 1, "disputed": False, "note": "clean"},
        "KX-B-T1": {"outcome": 0, "disputed": True,
                    "resolution_note": "UMA dispute reversed"},
    }
    stub = types.ModuleType("bot.connectors")
    stub.get_resolution = lambda venue, mid: resolutions.get(mid)
    monkeypatch.setitem(sys.modules, "bot.connectors", stub)

    summary = settle.settle_open(con, {})
    assert summary["checked"] == 4
    assert summary["settled"] == 2
    assert summary["disputed"] == 1
    assert summary["overdue"] == [r3]

    rows = {r["id"]: r for r in con.execute("SELECT * FROM forecasts")}
    assert rows[r1]["outcome"] == 1 and rows[r1]["disputed"] == 0
    # Disputed resolution is written, not dropped, with the note preserved.
    assert rows[r2]["outcome"] == 0 and rows[r2]["disputed"] == 1
    assert rows[r2]["resolution_note"] == "UMA dispute reversed"
    assert rows[r3]["outcome"] is None and rows[r4]["outcome"] is None

    # Second pass: settled rows are no longer open; overdue persists.
    summary2 = settle.settle_open(con, {})
    assert summary2["checked"] == 2 and summary2["settled"] == 0
    assert summary2["overdue"] == [r3]
