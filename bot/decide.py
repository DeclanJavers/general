"""Decision & sizing layer — maker-first (doc §2.4, Appendix B).

Converts (p, market book, fees) into an intended action. Evidence posture
(doc D.4): makers -9.6% vs takers -31.5% on Kalshi; sub-10c longshot taking
loses 60%+; so taker paths demand large edges and a side-of-bias gate, and
the default action on moderate edge is posting inside the spread.

PRICE-SPACE CONVENTION (shared with execute.py / position_pnl):
    Every price in a DecisionResult — including order_price for NO orders —
    is a YES-space probability in [0, 1]. A NO order at YES-space level x
    means: post/take a NO contract costing (1 - x). Sizing for NO uses the
    yes-space Kelly form (x - pt) / x. Downstream fill prices and P&L use
    the same convention: NO P&L per unit = (1-outcome) - (1-fill_price) - fee.

All fees are in probability points per contract (0.02 = 2 cents).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from bot.models import DecisionResult, Market

TICK = 0.01
_EPS = 1e-9


@dataclass
class PortfolioState:
    """Mutable risk state threaded through a run's decide() calls."""

    bankroll: float = 1.0
    daily_pnl: float = 0.0  # today's realized paper P&L, bankroll fraction
    cluster_exposure: dict = field(default_factory=dict)  # cluster_id -> worst-case

    def add(self, cluster_id, worst_case) -> None:
        self.cluster_exposure[cluster_id] = (
            self.cluster_exposure.get(cluster_id, 0.0) + worst_case)


def fee_points(price: float, fee_params: dict, maker: bool = False) -> float:
    """Fee in probability points per contract at `price` (yes-space).

    Models (fee_params["model"]):
      kalshi_quadratic: taker = ceil(rate*price*(1-price)*100)/100 — Kalshi
        rounds UP to the whole cent per contract (rate 0.07: 0.02 at 50c,
        0.01 at 10c). maker uses maker_rate (usually 0) in the same formula.
      flat (default):   fixed `rate` points, both sides.

    Symmetric in price <-> 1-price, so yes-space vs no-space doesn't matter.
    """
    fp = fee_params or {}
    if fp.get("model") == "kalshi_quadratic":
        rate = fp.get("maker_rate", 0.0) if maker else fp.get("rate", 0.0)
        return max(0.0, math.ceil(rate * price * (1 - price) * 100 - 1e-12) / 100)
    return fp.get("rate", 0.0)


def round_to_tick(x: float, tick: float = TICK, mode: str = "nearest") -> float:
    """Snap x to the price grid. mode: 'nearest' | 'down' | 'up'.

    decide() rounds maker levels *conservatively* (down for a yes bid, up for
    a no level) so the posted edge is never less than the configured one.
    """
    r = x / tick
    if mode == "down":
        n = math.floor(r + _EPS)
    elif mode == "up":
        n = math.ceil(r - _EPS)
    else:
        n = math.floor(r + 0.5 + _EPS)
    return round(n * tick, 10)


def _stake(pt: float, x: float, side: str, cfg: dict,
           portfolio: PortfolioState | None, cluster_id: str) -> float:
    """Fractional Kelly on the EXECUTION price x (never the mid), then caps.

    Returns the bankroll fraction to commit; <= 0 means the cluster cap is
    exhausted. Worst case of a binary position = the stake itself.
    """
    f_star = (pt - x) / (1 - x) if side == "yes" else (x - pt) / x
    f = cfg.get("kelly_fraction", 0.25) * f_star
    f = min(f, cfg.get("per_market_cap", 0.02))
    if portfolio is not None:
        room = cfg.get("per_cluster_cap", 0.05) - \
            portfolio.cluster_exposure.get(cluster_id, 0.0)
        f = min(f, room)
    return f


def _trade(side: str, order_type: str, x: float, pt: float, q_mid: float,
           cfg: dict, portfolio: PortfolioState | None, cluster_id: str,
           reason: str) -> DecisionResult:
    f = _stake(pt, x, side, cfg, portfolio, cluster_id)
    if f <= _EPS:
        return DecisionResult(side="pass", edge=pt - q_mid,
                              reason="cluster cap exhausted")
    if portfolio is not None:
        portfolio.add(cluster_id, f)
    return DecisionResult(side=side, order_type=order_type, order_price=x,
                          stake=f, edge=pt - q_mid, reason=reason)


def decide(p: float, market: Market, cfg: dict,
           portfolio: PortfolioState | None = None) -> DecisionResult:
    """Maker-first decision on one market. cfg = the `decision` config block.

    Order of gates:
      0. fresh two-sided book required; kill switch (portfolio.daily_pnl).
      1. shrink p toward the decision-time mid: pt = w*q_mid + (1-w)*p.
      2. theta hurdle from market.raw["theta_hurdle"] added to required edge
         on every path.
      3. taker paths (large edge only), behind the side-of-bias longshot gate:
         never take a contract priced under min_longshot_price (either side).
      4. maker paths: post strictly inside the spread at fair -/+ margin,
         conservatively tick-rounded.
      5. otherwise pass, with a reason naming the gate (for the explorer).
    """
    book = market.book
    if book is None or not book.bids or not book.asks:
        return DecisionResult(side="pass", reason="book one-sided/missing")
    if portfolio is not None and \
            portfolio.daily_pnl <= -cfg.get("daily_loss_kill", 0.10):
        return DecisionResult(side="pass", reason="kill switch")

    bid, ask, q_mid = book.best_bid, book.best_ask, book.mid
    w = cfg.get("shrink_to_market", 0.5)
    pt = w * q_mid + (1 - w) * p                       # shrunk belief
    hurdle = market.raw.get("theta_hurdle", 0.0)
    fp = market.fee_params
    cluster_id = market.cluster_id or market.key
    taker_edge = cfg.get("taker_edge", 0.03)
    min_ls = cfg.get("min_longshot_price", 0.10)

    # --- taker paths (evaluated first; only large edges take) ---
    fee_ask = fee_points(ask, fp, maker=False)
    if pt > ask + taker_edge + fee_ask + hurdle:
        if ask < min_ls:                               # side-of-bias gate
            return DecisionResult(side="pass", edge=pt - q_mid,
                                  reason="longshot-taker gate")
        return _trade("yes", "taker", ask, pt, q_mid, cfg, portfolio,
                      cluster_id, "taker: yes edge past threshold")
    fee_bid = fee_points(bid, fp, maker=False)
    if pt < bid - taker_edge - fee_bid - hurdle:
        if 1 - bid < min_ls:                           # NO contract sub-10c
            return DecisionResult(side="pass", edge=pt - q_mid,
                                  reason="longshot-taker gate")
        return _trade("no", "taker", bid, pt, q_mid, cfg, portfolio,
                      cluster_id, "taker: no edge past threshold")

    # --- maker paths: post strictly inside the spread ---
    maker_edge = cfg.get("maker_edge", 0.015)
    fee_mk = fee_points(pt, fp, maker=True)
    x_yes = round_to_tick(pt - maker_edge - fee_mk - hurdle, mode="down")
    x_no = round_to_tick(pt + maker_edge + fee_mk + hurdle, mode="up")
    # Prefer the side our shrunk fair leans toward; fall back to the other.
    order = ("yes", "no") if pt >= q_mid else ("no", "yes")
    for side in order:
        x = x_yes if side == "yes" else x_no
        if bid + _EPS < x < ask - _EPS:
            return _trade(side, "maker", x, pt, q_mid, cfg, portfolio,
                          cluster_id, f"maker: post {side} inside spread")

    return DecisionResult(side="pass", edge=pt - q_mid,
                          reason="maker level not strictly inside spread")
