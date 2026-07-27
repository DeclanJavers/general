"""Market connectors (§2.1): list markets, fetch books, poll resolutions.

Contracts:
  list_markets(venue, limit)     -> list[Market]   normalized, malformed rows skipped
  get_book(market)               -> Book           fresh full-depth book at decision time
  get_resolution(venue, mkt_id)  -> dict | None    {outcome, resolved_at, disputed, note}
  cached_get(url, params)        -> dict|list      HTTP GET with retries, rate limiting,
                                                   and append-only raw caching

Venues: Kalshi (primary; always mve_filter=exclude), Manifold (integration
testbed; AMM, so books are synthesized), Polymarket (read-only signal layer;
Gamma metadata is stale/string-encoded, CLOB for live books).

Every raw response body is archived gzip'd under data/raw/{venue}/{date}/ so
pipeline logic is replayable without re-hitting APIs. Prices are probabilities
in [0, 1]; timestamps ISO-8601 UTC.
"""
from __future__ import annotations

import gzip
import hashlib
import itertools
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from bot.models import Book, Market

log = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
MANIFOLD_BASE = "https://api.manifold.markets/v0"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
TIMEOUT = 15  # seconds per request
MANIFOLD_MIN_LIQ = 50.0  # cheap pre-filter: only fetch descriptions above this

# Politeness rate limits (requests/second), well under each venue's cap.
_RATE_LIMITS = {"kalshi": 10.0, "manifold": 5.0, "polymarket": 10.0}
_last_request: dict[str, float] = {}
_session = requests.Session()
_seq = itertools.count()  # uniquifies cache filenames within one microsecond


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- HTTP layer

def _venue_of(url: str) -> str:
    for needle, venue in (("kalshi", "kalshi"), ("manifold", "manifold"),
                          ("polymarket", "polymarket")):
        if needle in url:
            return venue
    return "other"


def _throttle(venue: str) -> None:
    rps = _RATE_LIMITS.get(venue, 5.0)
    wait = _last_request.get(venue, 0.0) + 1.0 / rps - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request[venue] = time.monotonic()


def _cache_raw(venue: str, url: str, params: dict | None, body: str) -> Path | None:
    """Append-only archive of one raw response body (gzip json). Never raises."""
    try:
        ts = datetime.now(timezone.utc)
        day_dir = RAW_DIR / venue / ts.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(
            f"{url}?{json.dumps(params or {}, sort_keys=True)}".encode()).hexdigest()[:16]
        path = day_dir / f"{key}_{ts.strftime('%Y%m%dT%H%M%S%f')}_{next(_seq)}.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(body)
        return path
    except OSError as e:  # a full disk must not kill the run
        log.warning("raw-cache write failed for %s: %s", url, e)
        return None


def load_raw(path: str | Path):
    """Read one archived response back (for replay)."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def cached_get(url: str, params: dict | None = None):
    """GET with session reuse, timeout, per-venue rate limiting, up to 3 retries
    with backoff on 429/5xx, and append-only raw caching. Returns parsed JSON."""
    venue = _venue_of(url)
    for attempt in range(4):  # 1 try + 3 retries
        _throttle(venue)
        try:
            resp = _session.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == 3:
                resp.raise_for_status()
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        _cache_raw(venue, url, params, resp.text)
        return resp.json()


# ------------------------------------------------------------ parse helpers

def _f(x) -> float | None:
    """Defensive float: None on anything unparseable."""
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _jloads(x):
    """Parse possibly-JSON-encoded values (Gamma ships numbers/lists as strings)."""
    if isinstance(x, str):
        try:
            return json.loads(x)
        except (TypeError, ValueError):
            return None
    return x


def _ms_iso(ms) -> str | None:
    """Epoch milliseconds (Manifold) -> ISO-8601 UTC."""
    ms = _f(ms)
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _kalshi_num(m: dict, field: str, cents: bool = False) -> float | None:
    """Prefer the *_dollars fixed-point string; fall back to the legacy numeric
    field (divided by 100 when it is a cents price). Deprecated fields like
    `liquidity` return 0 mid-migration — never rely on them."""
    v = _f(m.get(field + "_dollars"))
    if v is not None:
        return v
    v = _f(m.get(field))
    if v is None:
        return None
    return v / 100.0 if cents else v


def _top_book(bid: float | None, ask: float | None, qty: float = 0.0) -> Book:
    """Top-of-book snapshot from listing data (qty 0 = size unknown; refresh
    with get_book() at decision time)."""
    return Book(bids=[(bid, qty)] if bid else [],
                asks=[(ask, qty)] if ask else [], ts=_now_iso())


# ------------------------------------------------------------------- Kalshi

def _norm_kalshi(m: dict) -> Market:
    ticker = m["ticker"]
    criteria = "\n\n".join(t for t in (m.get("rules_primary"), m.get("rules_secondary")) if t)
    event = m.get("event_ticker") or ticker
    fee = {"model": "kalshi_quadratic",
           "taker_rate": _f(m.get("taker_fee_rate")) or _f(m.get("fee_rate")) or 0.07,
           "maker_rate": _f(m.get("maker_fee_rate")) or 0.0}
    return Market(
        venue="kalshi", market_id=ticker, question=m["title"],
        resolution_criteria=criteria,
        category=event.split("-")[0],  # series prefix of the event ticker
        close_time=m.get("close_time"),
        resolve_by=m.get("expected_expiration_time") or m.get("expiration_time")
        or m.get("close_time"),
        book=_top_book(_kalshi_num(m, "yes_bid", cents=True),
                       _kalshi_num(m, "yes_ask", cents=True)),
        fee_params=fee,
        volume_24h=_kalshi_num(m, "volume_24h"),
        open_interest=_kalshi_num(m, "open_interest"),
        raw=m)


def _list_kalshi(limit: int | None) -> list[Market]:
    out: list[Market] = []
    cursor = None
    while True:
        params = {"limit": min(1000, limit or 1000), "status": "open",
                  "mve_filter": "exclude"}  # without it, 99.3% is parlay junk
        if cursor:
            params["cursor"] = cursor
        data = cached_get(f"{KALSHI_BASE}/markets", params) or {}
        for m in data.get("markets") or []:
            mkt = _try_norm(_norm_kalshi, m, "kalshi")
            if mkt:
                out.append(mkt)
            if limit and len(out) >= limit:
                return out
        cursor = data.get("cursor")
        if not cursor:
            return out


def _kalshi_levels(levels) -> list[tuple[float, float]]:
    """One side of a Kalshi book: [price, qty] pairs, either legacy integer
    cents or newer `_fp` fixed-point dollar strings."""
    out = []
    for lv in levels or []:
        try:
            price, qty = lv[0], lv[1]
            p = float(price) if isinstance(price, str) else float(price) / 100.0
            out.append((p, float(qty)))
        except (TypeError, ValueError, IndexError) as e:
            log.warning("skipping malformed kalshi book level %r: %s", lv, e)
    return out


def _kalshi_book(market: Market) -> Book:
    data = cached_get(f"{KALSHI_BASE}/markets/{market.market_id}/orderbook") or {}
    ob = data.get("orderbook") or {}
    yes = _kalshi_levels(ob["yes_fp"] if ob.get("yes_fp") is not None else ob.get("yes"))
    no = _kalshi_levels(ob["no_fp"] if ob.get("no_fp") is not None else ob.get("no"))
    # Kalshi quotes YES bids and NO bids; a NO bid at x is a YES ask at 1-x.
    return Book(bids=sorted(yes, key=lambda l: -l[0]),
                asks=sorted(((round(1.0 - p, 6), q) for p, q in no), key=lambda l: l[0]),
                ts=_now_iso())


def _kalshi_resolution(market_id: str) -> dict | None:
    m = (cached_get(f"{KALSHI_BASE}/markets/{market_id}") or {}).get("market") or {}
    if m.get("status") not in ("settled", "finalized"):
        return None
    result = str(m.get("result") or "").lower()
    revised = any(m.get(k) for k in m if "revis" in k)  # settlement revision => disputed
    resolved_at = (m.get("settled_time") or m.get("settlement_time")
                   or m.get("close_time") or _now_iso())
    if result in ("yes", "no"):
        return {"outcome": 1 if result == "yes" else 0, "resolved_at": resolved_at,
                "disputed": bool(revised),
                "note": "settlement revised" if revised else ""}
    return {"outcome": 0, "resolved_at": resolved_at, "disputed": True,
            "note": f"non-binary result: {result!r}"}


# ----------------------------------------------------------------- Manifold

def _manifold_book(p: float | None, liq: float) -> Book:
    """Manifold is an AMM with no order book: synthesize one bid/ask around the
    AMM probability with a liquidity-implied half-spread so downstream spread/
    depth logic works. Displayed qty is the pool size (a proxy, not real depth)."""
    if p is None:
        return Book(ts=_now_iso())
    half = min(0.05, max(0.005, 2.0 / max(liq, 1.0)))
    qty = max(liq, 1.0)
    return Book(bids=[(max(p - half, 0.001), qty)],
                asks=[(min(p + half, 0.999), qty)], ts=_now_iso())


def _norm_manifold(m: dict, criteria: str) -> Market:
    liq = _f(m.get("totalLiquidity")) or 0.0
    return Market(
        venue="manifold", market_id=str(m["id"]), question=m["question"],
        resolution_criteria=criteria,
        close_time=_ms_iso(m.get("closeTime")),
        resolve_by=_ms_iso(m.get("closeTime")),
        book=_manifold_book(_f(m.get("probability")), liq),
        fee_params={"model": "none", "taker_rate": 0.0, "maker_rate": 0.0},
        volume_24h=_f(m.get("volume24Hours")),
        open_interest=liq or None,  # pool size is the closest analogue
        raw=m)


def _list_manifold(limit: int | None) -> list[Market]:
    out: list[Market] = []
    before = None
    page_size = min(1000, limit or 1000)
    while True:
        params = {"limit": page_size}
        if before:
            params["before"] = before
        page = cached_get(f"{MANIFOLD_BASE}/markets", params) or []
        if not page:
            return out
        for m in page:
            try:
                if m.get("outcomeType") != "BINARY" or m.get("isResolved"):
                    continue
                # Cheap pre-filter before the per-market detail call (500 req/min cap):
                # only markets with a real liquidity pool get their description.
                criteria = ""
                if (_f(m.get("totalLiquidity")) or 0.0) >= MANIFOLD_MIN_LIQ:
                    try:
                        detail = cached_get(f"{MANIFOLD_BASE}/market/{m['id']}") or {}
                        criteria = detail.get("textDescription") or ""
                    except Exception as e:
                        log.warning("manifold detail fetch failed for %s: %s",
                                    m.get("id"), e)
                out.append(_norm_manifold(m, criteria))
            except Exception as e:
                log.warning("skipping malformed manifold market %r: %s", m.get("id"), e)
            if limit and len(out) >= limit:
                return out
        if len(page) < page_size:
            return out
        before = page[-1].get("id")


def _manifold_live_book(market: Market) -> Book:
    m = cached_get(f"{MANIFOLD_BASE}/market/{market.market_id}") or {}
    return _manifold_book(_f(m.get("probability")), _f(m.get("totalLiquidity")) or 0.0)


def _manifold_resolution(market_id: str) -> dict | None:
    m = cached_get(f"{MANIFOLD_BASE}/market/{market_id}") or {}
    if not m.get("isResolved"):
        return None
    res = str(m.get("resolution") or "").upper()
    resolved_at = _ms_iso(m.get("resolutionTime")) or _now_iso()
    if res in ("YES", "NO"):
        return {"outcome": 1 if res == "YES" else 0, "resolved_at": resolved_at,
                "disputed": False, "note": ""}
    if res == "MKT":
        p = _f(m.get("resolutionProbability")) or 0.0
        return {"outcome": int(p >= 0.5), "resolved_at": resolved_at,
                "disputed": True, "note": f"resolved MKT at p={p}"}
    return {"outcome": 0, "resolved_at": resolved_at, "disputed": True,
            "note": f"resolution: {res or 'unknown'}"}


# --------------------------------------------------------------- Polymarket

def _norm_poly(m: dict) -> Market | None:
    """Normalize one Gamma market; returns None (silently) for non-binary ones.
    Gamma numeric fields arrive JSON-encoded in strings; prices are stale —
    treat the book as a hint and use get_book() (CLOB) at decision time."""
    outcomes = _jloads(m.get("outcomes")) or []
    if outcomes and [str(o).lower() for o in outcomes] != ["yes", "no"]:
        return None
    prices = [_f(x) for x in (_jloads(m.get("outcomePrices")) or [])]
    yes = prices[0] if prices else None
    bid = _f(m.get("bestBid"))
    ask = _f(m.get("bestAsk"))
    fee = {"model": "poly_quadratic",
           "taker_rate": _f(m.get("takerFeeRate")) or _f(m.get("fee")) or 0.0,
           "maker_rate": 0.0}
    return Market(
        venue="polymarket", market_id=str(m["id"]), question=m["question"],
        resolution_criteria=m.get("description") or "",
        category=m.get("category") or "",
        close_time=m.get("endDate"), resolve_by=m.get("endDate"),
        book=_top_book(bid if bid is not None else yes,
                       ask if ask is not None else yes),
        fee_params=fee,
        volume_24h=_f(m.get("volume24hr")),
        open_interest=_f(m.get("openInterest")),
        raw=m)


def _list_polymarket(limit: int | None) -> list[Market]:
    """Gamma listing. `limit` is silently capped at 100 and `offset` at ~5000,
    so deep scans use keyset pagination: ascending id order + last-seen id.
    A no-progress guard stops the loop if the cursor param is ever ignored."""
    out: list[Market] = []
    last_id = None
    while True:
        params = {"closed": "false", "limit": 100, "order": "id", "ascending": "true"}
        if last_id is not None:
            params["id_min"] = last_id + 1
        page = cached_get(f"{GAMMA_BASE}/markets", params)
        if not isinstance(page, list) or not page:
            return out
        max_id = None
        for m in page:
            mid = _f(m.get("id"))
            if mid is not None:
                max_id = mid if max_id is None else max(max_id, mid)
            mkt = _try_norm(_norm_poly, m, "polymarket")
            if mkt:
                out.append(mkt)
            if limit and len(out) >= limit:
                return out
        if max_id is None or (last_id is not None and max_id <= last_id):
            return out  # cursor not advancing — bail rather than loop forever
        last_id = int(max_id)
        if len(page) < 100:
            return out


def _clob_levels(levels) -> list[tuple[float, float]]:
    out = []
    for lv in levels or []:
        try:
            out.append((float(lv["price"]), float(lv["size"])))
        except (TypeError, ValueError, KeyError) as e:
            log.warning("skipping malformed clob level %r: %s", lv, e)
    return out


def _poly_book(market: Market) -> Book:
    tokens = _jloads(market.raw.get("clobTokenIds")) or []
    if not tokens:
        log.warning("polymarket %s has no clobTokenIds; empty book", market.market_id)
        return Book(ts=_now_iso())
    data = cached_get(f"{CLOB_BASE}/book", {"token_id": str(tokens[0])}) or {}
    return Book(bids=sorted(_clob_levels(data.get("bids")), key=lambda l: -l[0]),
                asks=sorted(_clob_levels(data.get("asks")), key=lambda l: l[0]),
                ts=_now_iso())


def _poly_resolution(market_id: str) -> dict | None:
    m = cached_get(f"{GAMMA_BASE}/markets/{market_id}")
    if isinstance(m, list):
        m = m[0] if m else {}
    m = m or {}
    if not m.get("closed"):
        return None
    prices = [_f(x) for x in (_jloads(m.get("outcomePrices")) or [])]
    if not prices or prices[0] is None:
        return None  # closed but not priced out yet
    statuses = [str(s).lower() for s in (_jloads(m.get("umaResolutionStatuses")) or [])]
    disputed = any("disput" in s or "challeng" in s for s in statuses)
    return {"outcome": 1 if prices[0] >= 0.5 else 0,
            "resolved_at": (m.get("closedTime") or m.get("umaEndDate")
                            or m.get("endDate") or _now_iso()),
            "disputed": disputed,
            "note": f"uma statuses: {statuses}" if disputed else ""}


# ----------------------------------------------------------------- dispatch

def _try_norm(fn, m, venue: str) -> Market | None:
    """Normalize one raw market; a malformed one is skipped with a warning,
    never an exception that kills the run."""
    try:
        return fn(m)
    except Exception as e:
        ident = (m or {}).get("ticker") or (m or {}).get("id")
        log.warning("skipping malformed %s market %r: %s", venue, ident, e)
        return None


_LISTERS = {"kalshi": _list_kalshi, "manifold": _list_manifold,
            "polymarket": _list_polymarket}
_BOOKS = {"kalshi": _kalshi_book, "manifold": _manifold_live_book,
          "polymarket": _poly_book}
_RESOLUTIONS = {"kalshi": _kalshi_resolution, "manifold": _manifold_resolution,
                "polymarket": _poly_resolution}


def list_markets(venue: str, limit: int | None = None) -> list[Market]:
    """All open (binary) markets on `venue`, normalized. `limit` caps the count."""
    if venue not in _LISTERS:
        raise ValueError(f"unknown venue: {venue!r}")
    return _LISTERS[venue](limit)


def get_book(market: Market) -> Book:
    """Fresh full-depth order book for one market, best-first, YES-side prices.
    (Kalshi NO bids are converted to YES asks; Manifold books are synthetic.)"""
    if market.venue not in _BOOKS:
        raise ValueError(f"unknown venue: {market.venue!r}")
    return _BOOKS[market.venue](market)


def get_resolution(venue: str, market_id: str) -> dict | None:
    """{"outcome": 0|1, "resolved_at": iso, "disputed": bool, "note": str},
    or None while unresolved. Ambiguous/voided/UMA-disputed outcomes come back
    with disputed=True so settlement can flag them for review."""
    if venue not in _RESOLUTIONS:
        raise ValueError(f"unknown venue: {venue!r}")
    return _RESOLUTIONS[venue](market_id)
