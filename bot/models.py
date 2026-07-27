"""Shared datatypes for the prediction-market bot.

Every module communicates through these types. Keep them stable: subagents,
the evaluator, and the dashboard all code against exactly these signatures.
All prices are probabilities in [0, 1] (a 37c Kalshi contract is 0.37).
All timestamps are ISO-8601 UTC strings.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Book:
    """Order-book snapshot. Levels are (price, qty) tuples, best-first.

    bids = prices at which you can SELL yes (buy no); asks = prices at which
    you can BUY yes. Empty lists mean that side is unquoted.
    """

    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)
    ts: str = ""

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.bids and self.asks:
            return (self.bids[0][0] + self.asks[0][0]) / 2
        return None

    @property
    def spread(self) -> float | None:
        if self.bids and self.asks:
            return self.asks[0][0] - self.bids[0][0]
        return None

    def depth(self, side: str) -> float:
        """Total displayed quantity on 'bid' or 'ask' side."""
        levels = self.bids if side == "bid" else self.asks
        return sum(q for _, q in levels)


@dataclass
class Market:
    """One normalized binary market from any venue."""

    venue: str  # 'kalshi' | 'manifold' | 'polymarket'
    market_id: str  # venue-native id/ticker
    question: str
    resolution_criteria: str
    category: str = ""  # venue category / series, used for segmentation
    close_time: str | None = None
    resolve_by: str | None = None
    book: Book | None = None
    fee_params: dict = field(default_factory=dict)  # venue fee model, AT SNAPSHOT TIME
    volume_24h: float | None = None
    open_interest: float | None = None
    cluster_id: str | None = None  # underlying real-world event; set by select.py
    raw: dict = field(default_factory=dict)  # raw venue payload (also cached to disk)

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.market_id}"


@dataclass
class ForecastResult:
    """Output of the blind forecasting harness for one market."""

    p: float  # aggregated, capped probability
    rationale: str
    sources: list[str] = field(default_factory=list)
    criteria_interp: str = ""  # resolution-criteria interpretation pass output
    ensemble_members: list[float] = field(default_factory=list)  # per-member raw p
    ensemble_spread: float = 0.0  # max - min of member p's
    retrieval_ok: bool = True  # False => retrieval was thin/failed; confidence capped
    contaminated: bool = False  # True => price-leak scan hit; excluded from headline
    cost_usd: float = 0.0


@dataclass
class DecisionResult:
    """Output of the decision layer for one (forecast, market) pair."""

    side: str  # 'yes' | 'no' | 'pass'
    order_type: str | None = None  # 'maker' | 'taker' | None when passing
    order_price: float | None = None  # limit price posted / level taken
    stake: float = 0.0  # bankroll fraction intended
    edge: float = 0.0  # p_shrunk - q_mid at decision time
    reason: str = ""  # why we passed / which gate fired (for the explorer)


@dataclass
class FillResult:
    """Output of the fill simulator for one decision."""

    price: float | None  # executable VWAP / limit price; None = unfilled
    qty_frac: float = 0.0  # fraction of intended size filled, in [0, 1]
    fee: float = 0.0  # fee paid in probability points at the fill price
