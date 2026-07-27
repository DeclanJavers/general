"""Paper execution / fill simulator — trade-through fills (doc §2.5, D.4).

Realism rules baked in (D.4): takers walk the book with a displayed-depth
cap and partial fills by default; makers fill ONLY on trade-through (touch
fills are fantasy in thin books) with an asymmetric adverse-selection
haircut; markouts are logged for every fill. Even so, expect live to run
20-50% below this sim.

PRICE-SPACE CONVENTION (same as decide.py / the log):
    All prices in and out of this module — FillResult.price, order_price,
    prints, mids — are YES-space probabilities. A NO fill at yes-space
    price x costs (1 - x) per contract; position_pnl() below applies that
    conversion, so callers never store no-space prices.

`cfg` everywhere = the `execution` block of config.yaml.
"""
from __future__ import annotations

from bot.decide import fee_points
from bot.models import Book, FillResult

_EPS = 1e-9


def sim_fill_taker(book: Book, side: str, size: float, fee_params: dict,
                   cfg: dict) -> FillResult:
    """Walk the snapshotted book best-first and fill at VWAP.

    'yes' walks the asks (buying YES); 'no' walks the bids best-first —
    highest bid first, i.e. cheapest NO contract first, since a bid at b is
    a NO offer at 1-b. Take is capped at cfg['depth_cap'] of displayed
    depth, applied per level (so the total cap is depth_cap of the side's
    depth). Partial fills are the default: qty_frac = filled / size.

    Returns FillResult with the yes-space VWAP; taker fee via
    decide.fee_points at that VWAP (fee is symmetric in price vs 1-price).
    """
    depth_cap = cfg.get("depth_cap", 0.25)
    levels = book.asks if side == "yes" else book.bids
    if not levels or size <= 0:
        return FillResult(price=None, qty_frac=0.0, fee=0.0)
    want = min(size, sum(q for _, q in levels) * depth_cap)
    filled = cost = 0.0
    for px, qty in levels:
        take = min(qty * depth_cap, want - filled)
        if take <= _EPS:
            break
        cost += take * px
        filled += take
    if filled <= _EPS:
        return FillResult(price=None, qty_frac=0.0, fee=0.0)
    vwap = cost / filled
    return FillResult(price=round(vwap, 10), qty_frac=filled / size,
                      fee=fee_points(vwap, fee_params, maker=False))


def sim_fill_maker(order_price: float, side: str, size: float,
                   prints_after: list[tuple[float, float]], drift: float,
                   cfg: dict) -> FillResult:
    """Maker fill from the tape — TRADE-THROUGH ONLY, never touch.

    Fill quantity = volume that actually printed at-or-through our level:
    prints at px <= order_price for a resting YES bid, px >= order_price for
    a NO order at yes-space level order_price (our YES offer).

    Adverse-selection asymmetry (D.4): `drift` is the signed yes-space mid
    move after posting. When it moved AGAINST our position (down for yes,
    up for no) we assume the full trade-through quantity fills us — we get
    filled precisely when we're wrong. When it moved our way, only
    cfg['adverse_fill_haircut'] of it does.

    Maker fee is 0 here (Kalshi maker_rate is 0 on most series); any nonzero
    maker fee is already priced into the level by decide().
    """
    haircut = cfg.get("adverse_fill_haircut", 0.4)
    if side == "yes":
        through = sum(q for px, q in prints_after if px <= order_price + _EPS)
        adverse = drift < 0
    else:
        through = sum(q for px, q in prints_after if px >= order_price - _EPS)
        adverse = drift > 0
    filled = min(size, through) * (1.0 if adverse else haircut)
    if size <= 0 or filled <= _EPS:
        return FillResult(price=None, qty_frac=0.0, fee=0.0)
    return FillResult(price=order_price, qty_frac=filled / size, fee=0.0)


def estimate_maker_fill(order_price: float, side: str, book_later: Book,
                        cfg: dict) -> FillResult:
    """Paper-mode maker fill estimate WITHOUT a tape.

    *** OPTIMISTIC APPROXIMATION — a stand-in, not a fill model. ***
    With no prints, all we can see is whether a later book snapshot traded
    through our level: later best_ask <= our yes bid (yes side), or later
    best_bid >= our yes-space level (no side). That observation carries no
    volume, so when it trades through we credit only a flat
    cfg['adverse_fill_haircut'] fraction of the order; when it doesn't, no
    fill. This overstates fills (a crossed later book doesn't prove volume
    printed at our price) and understates them (prints between snapshots are
    invisible). Prefer sim_fill_maker() with real prints whenever available.
    """
    haircut = cfg.get("adverse_fill_haircut", 0.4)
    if side == "yes":
        hit = book_later.best_ask is not None and \
            book_later.best_ask <= order_price + _EPS
    else:
        hit = book_later.best_bid is not None and \
            book_later.best_bid >= order_price - _EPS
    if not hit:
        return FillResult(price=None, qty_frac=0.0, fee=0.0)
    return FillResult(price=order_price, qty_frac=haircut, fee=0.0)


def compute_markout(fill_price: float | None, side: str,
                    mid_later: float | None) -> float | None:
    """Signed yes-space mid move after our fill: + = in our favor, - = against.

    yes: mid_later - fill_price;  no: fill_price - mid_later.
    Systematically negative markouts = toxic-selected flow (widen the
    toxicity buffer in decide). None if there was no fill or no later mid.
    """
    if fill_price is None or mid_later is None:
        return None
    move = mid_later - fill_price
    return move if side == "yes" else -move


def _get(row, key, default=None):
    """Field access for dicts and sqlite3.Row alike."""
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def position_pnl(row) -> float | None:
    """Realized paper P&L of one settled log row, in bankroll fraction.

    Per unit (all yes-space, fees in probability points):
        yes: outcome - fill_price - fee
        no:  (1 - outcome) - (1 - fill_price) - fee
    scaled by stake * fill_qty_frac. None for passes, unfilled orders, or
    unresolved rows.
    """
    side = _get(row, "side")
    fill_price = _get(row, "fill_price")
    qty = _get(row, "fill_qty_frac", 0.0)
    outcome = _get(row, "outcome")
    if side not in ("yes", "no") or fill_price is None or qty <= 0 \
            or outcome is None:
        return None
    fee = _get(row, "fee", 0.0)
    stake = _get(row, "stake", 0.0)
    if side == "yes":
        unit = outcome - fill_price - fee
    else:
        unit = (1 - outcome) - (1 - fill_price) - fee
    return unit * stake * qty
