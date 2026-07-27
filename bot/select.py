"""Selection funnel — the most important component (doc §2.2).

Turns the ingested universe into a ranked shortlist worth the expensive
harness. Hard gates in order: vertical -> resolution clarity (hardest) ->
time-to-resolution -> liquidity/tradability -> theta annotation -> rank.
Each gate is a small pure function so it can be tested in isolation.

Cluster IDs (the underlying real-world event) are assigned here, at
selection time, because they cannot be reconstructed later (doc §2.6).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from bot.models import Market

# Hurdle above which theta alone kills the trade (annotate-don't-drop cutoff).
THETA_DROP = 0.10
# Minimum resolution-criteria length considered "reasonable".
MIN_CRITERIA_LEN = 40

# --- vertical gate -----------------------------------------------------------
# cfg token -> (category substrings, question-keyword regex). Doc §2.2 "avoid":
# Fed/CPI/rates + index levels, election toplines, sports mainlines, crypto
# price levels, and mve parlay legs (category-only match).
_EXCLUDE = {
    "fed": (("fed", "rates", "monetary"),
            r"\bfed\b|\bfomc\b|federal reserve|interest rates?|rate (?:hike|cut)"
            r"|s&p|nasdaq|dow jones|treasury yield"),
    "cpi": (("cpi", "inflation"),
            r"\bcpi\b|consumer price index|inflation rate"),
    "elections_top": (("election",),
                      r"presidential election|win the presidency|next president"
                      r"|electoral college|control of the (?:senate|house)"),
    "sports_main": (("sports",),
                    r"super bowl|world series|world cup|premier league"
                    r"|\b(?:nfl|nba|mlb|nhl)\b"),
    "crypto_price": (("crypto",),
                     r"(?:bitcoin|btc|ethereum|eth|solana|dogecoin)\s+"
                     r"(?:price|above|below|reach|hit)"
                     r"|price of (?:bitcoin|btc|ethereum|eth)"),
    "mve": (("mve", "multivariate"), r"$^"),
}

# Preferred (Tier 1-3) categories earn a rank bonus, not a hard gate:
# procedural/legislative, regulatory/legal, corporate/product timing,
# awards/pop-culture, science/space timing.
_PREFER_RE = re.compile(
    r"\b(?:bill|act|congress|senate|legislation|veto|statute|regulation|ruling"
    r"|court|lawsuit|filing|docket|sec|fda|fcc|ftc|epa|executive order"
    r"|confirm(?:ed|ation)?|nomin(?:ee|ation)|ipo|merger|acquisition|recall"
    r"|launch|release|ship|version|award|oscar|grammy|emmy|nobel)\b", re.I)

# --- clarity gate ------------------------------------------------------------
_SOURCE_RE = re.compile(
    r"according to|\.gov\b|official|press release|federal register"
    r"|congressional record|supreme court|white house|department of|bureau of"
    r"|associated press|reuters|\bnasa\b|\bfda\b|\bsec\b|8-k|10-k"
    r"|as (?:published|reported) by", re.I)
# "may" deliberately absent from the month list (too many false positives).
_DATE_RE = re.compile(
    r"\b(?:january|february|march|april|june|july|august|september|october"
    r"|november|december)\b|\d{4}-\d{2}-\d{2}"
    r"|\b(?:by|before|on or before)\b.{0,40}\b\d{4}\b|\bdeadline\b"
    r"|11:59|\bmidnight\b|\b(?:et|utc)\b", re.I)
_RED_FLAGS = ("in the opinion of", "substantially", "widely reported",
              "consensus", "credible report", "at the discretion",
              "in the judgment of", "generally understood")


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def days_to_resolution(m: Market, now: datetime | None = None) -> float | None:
    """Days until resolve_by (close_time fallback); None if neither parses."""
    dt = _parse_ts(m.resolve_by) or _parse_ts(m.close_time)
    if dt is None:
        return None
    return (dt - _now(now)).total_seconds() / 86400.0


def vertical_ok(m: Market, categories_exclude: list[str]) -> bool:
    """Drop excluded categories, matching category string AND question text."""
    cat, q = m.category.lower(), m.question.lower()
    for token in categories_exclude:
        cats, pat = _EXCLUDE.get(token, ((token.replace("_", " "),), r"$^"))
        if any(c in cat for c in cats) or re.search(pat, q):
            return False
    return True


def prefer_bonus(m: Market) -> int:
    """1 if the market looks procedural/regulatory/timing/awards (Tier 1-3)."""
    return 1 if _PREFER_RE.search(m.question + " " + m.category) else 0


def clarity_score(m: Market) -> int:
    """Heuristic clarity: named source (+1, missing -1), explicit date (+1),
    each ambiguity red flag (-1). Max attainable = 2."""
    text = m.resolution_criteria
    score = 1 if _SOURCE_RE.search(text) else -1
    if _DATE_RE.search(text):
        score += 1
    low = text.lower()
    score -= sum(1 for f in _RED_FLAGS if f in low)
    return score


def clarity_ok(m: Market) -> bool:
    """The hardest gate (doc §2.2 #2). Polymarket => higher bar (UMA risk)."""
    if len(m.resolution_criteria.strip()) < MIN_CRITERIA_LEN:
        return False
    return clarity_score(m) >= (2 if m.venue == "polymarket" else 1)


def time_ok(m: Market, cfg: dict, now: datetime | None = None) -> bool:
    days = days_to_resolution(m, now)
    if days is None:  # missing both resolve_by and close_time
        return False
    return cfg.get("min_days_to_resolution", 0) <= days <= \
        cfg.get("max_days_to_resolution", 90)


def liquidity_ok(m: Market, cfg: dict) -> bool:
    """Two-sided book, spread <= cap, nonzero displayed depth both sides."""
    b = m.book
    if b is None:
        return False
    sides = bool(b.bids) + bool(b.asks)
    if sides < cfg.get("min_book_sides", 2):
        return False
    if b.spread is None or b.spread > cfg.get("max_spread", 0.05):
        return False
    return b.depth("bid") > 0 and b.depth("ask") > 0


def theta_ok(m: Market, cfg: dict, now: datetime | None = None) -> bool:
    """Annotate raw['theta_hurdle'] for the decision layer; drop only when the
    hurdle alone exceeds THETA_DROP (doc §2.2 #4: theta is priced)."""
    days = days_to_resolution(m, now) or 0.0
    hurdle = cfg.get("theta_hurdle_annual", 0.05) * days / 365.0
    m.raw["theta_hurdle"] = hurdle
    return hurdle <= THETA_DROP


# --- clustering --------------------------------------------------------------
_MONTHS_RE = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun[e]?|jul[y]?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?"
    r"|dec(?:ember)?)\b", re.I)
_NUM_RE = re.compile(r"[$€£]?\d[\d,.]*\s*[%kmb]?", re.I)
_STOP = {"will", "the", "a", "an", "by", "before", "after", "in", "on", "of",
         "to", "at", "be", "is", "or", "and", "end", "than", "q1", "q2", "q3",
         "q4", "this", "next", "year"}


def _slug(question: str) -> str:
    """Slug with dates/numbers/thresholds stripped so 'by March?'/'by June?'
    variants of the same subject cluster together."""
    q = _NUM_RE.sub(" ", _MONTHS_RE.sub(" ", question.lower()))
    q = re.sub(r"[^a-z\s]", " ", q)
    words = [w for w in q.split() if w not in _STOP and len(w) > 1]
    return "-".join(words) or "unknown"


def assign_cluster(market: Market) -> str:
    """Underlying real-world event ID; same-event markets count once."""
    if market.venue == "kalshi":
        event = market.raw.get("event_ticker")
        if not event:
            parts = market.market_id.split("-")
            # "KXBILL-25XYZ-T3" -> "KXBILL-25XYZ"; keep short tickers whole.
            event = "-".join(parts[:-1]) if len(parts) >= 3 else market.market_id
        return f"kalshi:{event}"
    return f"{market.venue}:{_slug(market.question)}"


# --- funnel ------------------------------------------------------------------
def _rank_key(m: Market, now: datetime | None):
    """Higher clarity + category bonus first, then sooner, then tighter."""
    return (-(clarity_score(m) + prefer_bonus(m)),
            days_to_resolution(m, now) or 0.0,
            m.book.spread if m.book else 1.0)


def run_funnel(markets: list[Market], cfg: dict,
               now: datetime | None = None) -> tuple[list[Market], dict]:
    """Apply the gates in order; return (shortlist, per-gate survivor counts).

    cfg is the `selection` block of config.yaml. Counts feed the funnel panel.
    """
    now = _now(now)
    counts = {"ingested": len(markets)}
    excl = cfg.get("categories_exclude", [])
    s = [m for m in markets if vertical_ok(m, excl)]
    counts["vertical"] = len(s)
    s = [m for m in s if clarity_ok(m)]
    counts["clarity"] = len(s)
    s = [m for m in s if time_ok(m, cfg, now)]
    counts["time"] = len(s)
    s = [m for m in s if liquidity_ok(m, cfg)]
    counts["liquidity"] = len(s)
    s = [m for m in s if theta_ok(m, cfg, now)]
    counts["theta"] = len(s)
    s.sort(key=lambda m: _rank_key(m, now))
    shortlist = s[:cfg.get("shortlist_size", 25)]
    for m in shortlist:
        m.cluster_id = assign_cluster(m)
    counts["shortlist"] = len(shortlist)
    return shortlist, counts
