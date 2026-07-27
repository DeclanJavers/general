"""Tests for the decision layer (bot/decide.py) and fill sim (bot/execute.py).

Synthetic Book/Market objects only — no network, no database.
All prices yes-space (see decide.py convention note).
"""
from __future__ import annotations

import pytest

from bot.decide import PortfolioState, decide, fee_points, round_to_tick
from bot.execute import (compute_markout, estimate_maker_fill, position_pnl,
                         sim_fill_maker, sim_fill_taker)
from bot.models import Book, Market

# Mirrors config.yaml `decision` / `execution` blocks (fixed contracts).
CFG = dict(kelly_fraction=0.25, shrink_to_market=0.5, maker_edge=0.015,
           taker_edge=0.03, min_longshot_price=0.10, per_market_cap=0.02,
           per_cluster_cap=0.05, daily_loss_kill=0.10)
ECFG = dict(depth_cap=0.25, adverse_fill_haircut=0.4, staleness_seconds=60)
KFEE = dict(model="kalshi_quadratic", rate=0.07, maker_rate=0.0)


def mkt(bid=None, ask=None, bid_q=100.0, ask_q=100.0, fee=KFEE, **raw):
    book = Book(bids=[(bid, bid_q)] if bid is not None else [],
                asks=[(ask, ask_q)] if ask is not None else [])
    return Market(venue="kalshi", market_id="M1", question="?",
                  resolution_criteria="", book=book, fee_params=dict(fee),
                  cluster_id="c1", raw=raw)


# ---------------------------------------------------------------- fees ----

def test_fee_points_kalshi_quadratic_ceils_to_whole_cent():
    assert fee_points(0.5, KFEE, maker=False) == pytest.approx(0.02)
    assert fee_points(0.1, KFEE, maker=False) == pytest.approx(0.01)
    assert fee_points(0.9, KFEE, maker=False) == pytest.approx(0.01)
    assert fee_points(0.5, KFEE, maker=True) == 0.0          # maker_rate 0


def test_fee_points_flat_model_both_sides():
    flat = dict(model="flat", rate=0.007)
    assert fee_points(0.5, flat, maker=False) == 0.007
    assert fee_points(0.1, flat, maker=True) == 0.007
    assert fee_points(0.5, {"rate": 0.01}) == 0.01           # default = flat
    assert fee_points(0.5, {}) == 0.0
    assert fee_points(0.5, None) == 0.0


# --------------------------------------------------------------- taker ----

def test_taker_yes_triggers_only_past_threshold_fee_hurdle():
    # bid .48 / ask .50, q_mid .49; fee at ask = .02 => need pt > .55
    m = mkt(0.48, 0.50)
    d = decide(0.72, m, CFG)              # pt = .605 > .55
    assert (d.side, d.order_type, d.order_price) == ("yes", "taker", 0.50)
    d = decide(0.60, m, CFG)              # pt = .545 <= .55
    assert d.order_type != "taker"


def test_taker_no_triggers_only_past_threshold():
    # fee at bid .48 = .02 => need pt < .48 - .03 - .02 = .43
    m = mkt(0.48, 0.50)
    d = decide(0.30, m, CFG)              # pt = .395 < .43
    assert (d.side, d.order_type) == ("no", "taker")
    assert d.order_price == 0.48          # yes-space level; NO costs 1-.48
    d = decide(0.40, m, CFG)              # pt = .445 >= .43
    assert d.order_type != "taker"


def test_theta_hurdle_raises_taker_requirement():
    m0 = mkt(0.48, 0.50)
    mh = mkt(0.48, 0.50, theta_hurdle=0.05)  # need pt > .60 now
    d = decide(0.70, m0, CFG)             # pt = .595 > .55: takes
    assert d.order_type == "taker"
    d = decide(0.70, mh, CFG)             # .595 < .60: hurdle blocks
    assert d.order_type != "taker"


def test_longshot_gate_blocks_sub_10c_takes_both_sides():
    # YES taker at ask .08 (<.10) despite huge edge
    d = decide(0.90, mkt(0.05, 0.08), CFG)
    assert d.side == "pass" and d.reason == "longshot-taker gate"
    # NO taker where NO contract costs 1-.95 = .05 (<.10)
    d = decide(0.10, mkt(0.95, 0.97), CFG)
    assert d.side == "pass" and d.reason == "longshot-taker gate"
    # same setup but cheap side >= .10 does take
    d = decide(0.90, mkt(0.08, 0.11), CFG)
    assert (d.side, d.order_type) == ("yes", "taker")


# --------------------------------------------------------------- maker ----

def test_maker_posts_strictly_inside_spread_correct_side_of_fair():
    m = mkt(0.40, 0.60)                   # q_mid .50, maker fee 0
    d = decide(0.60, m, CFG)              # pt = .55 > mid -> yes bid
    assert (d.side, d.order_type) == ("yes", "maker")
    assert 0.40 < d.order_price < 0.60    # strictly inside
    assert d.order_price <= 0.55 - CFG["maker_edge"] + 1e-9  # below fair - edge
    d = decide(0.40, m, CFG)              # pt = .45 < mid -> no side
    assert (d.side, d.order_type) == ("no", "maker")
    assert 0.40 < d.order_price < 0.60
    assert d.order_price >= 0.45 + CFG["maker_edge"] - 1e-9  # above fair + edge


def test_maker_level_outside_spread_passes_with_reason():
    m = mkt(0.48, 0.50)                   # 2c spread: no room inside
    d = decide(0.60, m, CFG)              # pt=.545; level .53 >= ask
    assert d.side == "pass" and "spread" in d.reason


def test_round_to_tick_modes():
    assert round_to_tick(0.535, mode="down") == pytest.approx(0.53)
    assert round_to_tick(0.535, mode="up") == pytest.approx(0.54)
    assert round_to_tick(0.53, mode="down") == pytest.approx(0.53)  # on-grid
    assert round_to_tick(0.53, mode="up") == pytest.approx(0.53)


# -------------------------------------------------------------- sizing ----

def test_sizing_uses_execution_price_not_mid():
    m = mkt(0.48, 0.50)
    cfg = {**CFG, "per_market_cap": 1.0}  # uncap to see the raw Kelly stake
    d = decide(0.90, m, cfg)              # pt = .695, taker yes at x = .50
    kelly_at_exec = 0.25 * (0.695 - 0.50) / (1 - 0.50)
    kelly_at_mid = 0.25 * (0.695 - 0.49) / (1 - 0.49)
    assert d.stake == pytest.approx(kelly_at_exec)
    assert d.stake != pytest.approx(kelly_at_mid)


def test_per_market_cap_clips_stake():
    d = decide(0.90, mkt(0.48, 0.50), CFG)
    assert d.stake == pytest.approx(CFG["per_market_cap"])


def test_per_cluster_cap_clips_and_accumulates():
    pf = PortfolioState(cluster_exposure={"c1": 0.045})
    d = decide(0.90, mkt(0.48, 0.50), CFG, pf)
    assert d.stake == pytest.approx(0.005)          # only .005 of .05 left
    assert pf.cluster_exposure["c1"] == pytest.approx(0.05)
    # exhausted cluster -> pass
    d = decide(0.90, mkt(0.48, 0.50), CFG, pf)
    assert d.side == "pass" and "cluster" in d.reason


def test_kill_switch():
    pf = PortfolioState(daily_pnl=-0.10)
    d = decide(0.90, mkt(0.48, 0.50), CFG, pf)
    assert d.side == "pass" and d.reason == "kill switch"
    pf = PortfolioState(daily_pnl=-0.05)            # above the kill level
    assert decide(0.90, mkt(0.48, 0.50), CFG, pf).side == "yes"


def test_one_sided_or_missing_book_passes():
    d = decide(0.90, mkt(bid=0.48, ask=None), CFG)
    assert d.side == "pass" and "book" in d.reason
    m = mkt(0.48, 0.50)
    m.book = None
    assert decide(0.90, m, CFG).side == "pass"


# ------------------------------------------------------ sim_fill_taker ----

def test_sim_fill_taker_vwap_depth_cap_partial():
    book = Book(asks=[(0.50, 100.0), (0.55, 100.0)], bids=[(0.45, 100.0)])
    r = sim_fill_taker(book, "yes", 60.0, KFEE, ECFG)
    # cap = 25% of 200 = 50: 25 @ .50 + 25 @ .55 -> VWAP .525, partial 50/60
    assert r.price == pytest.approx(0.525)
    assert r.qty_frac == pytest.approx(50.0 / 60.0)
    assert r.fee == pytest.approx(0.02)   # ceil(.07*.525*.475*100)/100


def test_sim_fill_taker_no_side_walks_bids_yes_space_price():
    book = Book(bids=[(0.60, 100.0), (0.55, 100.0)], asks=[(0.65, 100.0)])
    r = sim_fill_taker(book, "no", 10.0, KFEE, ECFG)
    assert r.price == pytest.approx(0.60)  # yes-space; NO cost = .40
    assert r.qty_frac == pytest.approx(1.0)


def test_sim_fill_taker_empty_side_unfilled():
    r = sim_fill_taker(Book(bids=[(0.45, 10.0)]), "yes", 5.0, KFEE, ECFG)
    assert r.price is None and r.qty_frac == 0.0


# ------------------------------------------------------ sim_fill_maker ----

def test_maker_trade_through_fills_touch_does_not():
    prints = [(0.44, 10.0), (0.46, 5.0)]            # 10 through a .45 yes bid
    r = sim_fill_maker(0.45, "yes", 8.0, prints, drift=-0.02, cfg=ECFG)
    assert r.price == 0.45 and r.qty_frac == pytest.approx(1.0)
    # prints only above our bid (touch, not through) -> no fill
    r = sim_fill_maker(0.45, "yes", 8.0, [(0.46, 50.0)], drift=-0.02, cfg=ECFG)
    assert r.price is None and r.qty_frac == 0.0


def test_maker_adverse_selection_asymmetry():
    prints = [(0.44, 10.0)]
    full = sim_fill_maker(0.45, "yes", 8.0, prints, drift=-0.02, cfg=ECFG)
    hair = sim_fill_maker(0.45, "yes", 8.0, prints, drift=+0.02, cfg=ECFG)
    assert full.qty_frac == pytest.approx(1.0)      # moved against us: filled
    assert hair.qty_frac == pytest.approx(ECFG["adverse_fill_haircut"])


def test_maker_no_side_trade_through_and_asymmetry():
    prints = [(0.56, 6.0), (0.50, 4.0)]             # 6 through .55 yes-offer
    r = sim_fill_maker(0.55, "no", 5.0, prints, drift=+0.03, cfg=ECFG)
    assert r.qty_frac == pytest.approx(1.0)         # up-drift hurts NO: full
    r = sim_fill_maker(0.55, "no", 5.0, prints, drift=-0.03, cfg=ECFG)
    assert r.qty_frac == pytest.approx(0.4 * 5.0 / 5.0 * 1.0) or \
        r.qty_frac == pytest.approx(0.4)


# --------------------------------------------------- estimate_maker_fill ----

def test_estimate_maker_fill_both_directions():
    # yes bid .45: later ask traded down through it
    r = estimate_maker_fill(0.45, "yes", Book(asks=[(0.44, 5.0)]), ECFG)
    assert r.price == 0.45 and r.qty_frac == pytest.approx(0.4)
    r = estimate_maker_fill(0.45, "yes", Book(asks=[(0.46, 5.0)]), ECFG)
    assert r.price is None and r.qty_frac == 0.0
    # no order at yes-space .55: later bid traded up through it
    r = estimate_maker_fill(0.55, "no", Book(bids=[(0.56, 5.0)]), ECFG)
    assert r.price == 0.55 and r.qty_frac == pytest.approx(0.4)
    r = estimate_maker_fill(0.55, "no", Book(bids=[(0.54, 5.0)]), ECFG)
    assert r.price is None
    # missing later book side -> no fill
    assert estimate_maker_fill(0.45, "yes", Book(), ECFG).price is None


# ------------------------------------------------------------- markout ----

def test_markout_sign_convention():
    assert compute_markout(0.50, "yes", 0.53) == pytest.approx(+0.03)
    assert compute_markout(0.50, "yes", 0.47) == pytest.approx(-0.03)
    assert compute_markout(0.50, "no", 0.53) == pytest.approx(-0.03)
    assert compute_markout(0.50, "no", 0.47) == pytest.approx(+0.03)
    assert compute_markout(None, "yes", 0.53) is None
    assert compute_markout(0.50, "yes", None) is None


# --------------------------------------------------------- position_pnl ----

def _row(**kw):
    base = dict(side="yes", fill_price=0.60, fill_qty_frac=1.0,
                stake=0.02, fee=0.02, outcome=1)
    base.update(kw)
    return base


def test_position_pnl_yes_no_win_loss():
    assert position_pnl(_row()) == pytest.approx(0.02 * (1 - 0.60 - 0.02))
    assert position_pnl(_row(outcome=0)) == pytest.approx(0.02 * (0 - 0.60 - 0.02))
    assert position_pnl(_row(side="no", outcome=0)) == \
        pytest.approx(0.02 * (1 - (1 - 0.60) - 0.02))
    assert position_pnl(_row(side="no", outcome=1)) == \
        pytest.approx(0.02 * (0 - (1 - 0.60) - 0.02))
    # partial fill scales
    assert position_pnl(_row(fill_qty_frac=0.5)) == \
        pytest.approx(0.5 * 0.02 * (1 - 0.60 - 0.02))


def test_position_pnl_none_for_pass_unfilled_unresolved():
    assert position_pnl(_row(side="pass", fill_price=None)) is None
    assert position_pnl(_row(fill_price=None)) is None      # maker unfilled
    assert position_pnl(_row(fill_qty_frac=0.0)) is None
    assert position_pnl(_row(outcome=None)) is None
