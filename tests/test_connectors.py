"""Tests for bot/connectors.py — fixture-driven, no live network by default.

Live smoke tests at the bottom run only when RUN_LIVE is set in the env:
    RUN_LIVE=1 python3 -m pytest tests/test_connectors.py -q
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest
import requests

from bot import connectors
from bot.models import Book, Market

FIXTURES = Path(__file__).parent / "fixtures"


def fx(name):
    return json.loads((FIXTURES / name).read_text())


class FakeAPI:
    """Stands in for connectors.cached_get; routes on URL substring (first match)."""

    def __init__(self):
        self.routes: list[tuple[str, object]] = []
        self.calls: list[tuple[str, dict]] = []

    def add(self, substr, payload):
        self.routes.append((substr, payload))
        return self

    def __call__(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        for substr, payload in self.routes:
            if substr in url:
                return copy.deepcopy(payload)
        raise AssertionError(f"unexpected URL in test: {url}")


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI()
    monkeypatch.setattr(connectors, "cached_get", fake)
    return fake


# ------------------------------------------------------------------- Kalshi

def test_kalshi_list_params_and_normalization(api):
    api.add("/markets", fx("kalshi_markets_page.json"))
    markets = connectors.list_markets("kalshi")

    url, params = api.calls[0]
    assert params["mve_filter"] == "exclude"
    assert params["status"] == "open"
    assert params["limit"] <= 1000

    assert len(markets) == 2  # malformed third row skipped, not raised

    m = markets[0]  # dollars-variant market
    assert m.venue == "kalshi" and m.market_id == "KXFED-26SEP-T3.75"
    assert m.category == "KXFED"  # series prefix of event_ticker
    assert "federal funds target range" in m.resolution_criteria
    assert "source agency" in m.resolution_criteria  # secondary rules appended
    # *_dollars preferred over deprecated zeroed cents fields
    assert m.book.best_bid == pytest.approx(0.37)
    assert m.book.best_ask == pytest.approx(0.41)
    assert m.fee_params == {"model": "kalshi_quadratic",
                            "taker_rate": 0.07, "maker_rate": 0.0}
    assert m.volume_24h == 1220 and m.open_interest == 4500
    assert m.close_time == "2026-09-17T18:00:00Z"
    assert m.resolve_by == "2026-09-18T00:00:00Z"

    m2 = markets[1]  # cents-only market, per-series fee rate from API
    assert m2.book.best_bid == pytest.approx(0.12)
    assert m2.book.best_ask == pytest.approx(0.15)
    assert m2.fee_params["taker_rate"] == pytest.approx(0.035)
    assert m2.category == "KXOSCARPIC"


def test_kalshi_list_respects_limit(api):
    api.add("/markets", fx("kalshi_markets_page.json"))
    assert len(connectors.list_markets("kalshi", limit=1)) == 1


def _kalshi_market():
    return Market(venue="kalshi", market_id="KXFED-26SEP-T3.75",
                  question="q", resolution_criteria="r")


def test_kalshi_orderbook_cents_and_no_side_conversion(api):
    api.add("/orderbook", fx("kalshi_orderbook_cents.json"))
    book = connectors.get_book(_kalshi_market())
    assert book.bids == [(0.35, 100.0), (0.34, 50.0)]  # best-first
    # NO bids at 0.60/0.58 become YES asks at 0.40/0.42
    assert book.asks == [(0.40, 80.0), (0.42, 40.0)]
    assert book.mid == pytest.approx(0.375)


def test_kalshi_orderbook_fp_dollar_strings(api):
    api.add("/orderbook", fx("kalshi_orderbook_fp.json"))
    book = connectors.get_book(_kalshi_market())
    assert book.bids == [(0.35, 100.0), (0.34, 50.0)]
    assert book.asks == [(0.40, 80.0), (0.42, 40.0)]


def test_kalshi_resolution_settled_yes(api):
    api.add("/markets/", fx("kalshi_market_settled.json"))
    res = connectors.get_resolution("kalshi", "KXFED-26SEP-T3.75")
    assert res == {"outcome": 1, "resolved_at": "2026-09-18T01:12:44Z",
                   "disputed": False, "note": ""}


def test_kalshi_resolution_unresolved_and_disputed(api):
    still_open = fx("kalshi_market_settled.json")
    still_open["market"]["status"] = "open"
    api.add("open-mkt", still_open)

    voided = fx("kalshi_market_settled.json")
    voided["market"]["result"] = "void"
    api.add("void-mkt", voided)

    revised = fx("kalshi_market_settled.json")
    revised["market"]["settlement_revision_count"] = 1
    api.add("revised-mkt", revised)

    assert connectors.get_resolution("kalshi", "open-mkt") is None
    res = connectors.get_resolution("kalshi", "void-mkt")
    assert res["disputed"] is True and "void" in res["note"]
    res = connectors.get_resolution("kalshi", "revised-mkt")
    assert res["outcome"] == 1 and res["disputed"] is True


# ----------------------------------------------------------------- Manifold

def test_manifold_binary_filter_and_synthetic_book(api):
    api.add("/v0/market/", fx("manifold_market_detail.json"))
    api.add("/v0/markets", fx("manifold_markets.json"))
    markets = connectors.list_markets("manifold")

    # multiple-choice and resolved markets filtered out
    assert [m.market_id for m in markets] == ["mf1abc", "mf3tiny"]

    m = markets[0]
    assert m.venue == "manifold"
    # synthetic AMM book straddles the probability with a small half-spread
    assert m.book.best_bid < 0.62 < m.book.best_ask
    assert m.book.spread <= 0.10
    assert m.book.mid == pytest.approx(0.62)
    assert m.book.depth("bid") > 0 and m.book.depth("ask") > 0
    # description fetched for the market passing the liquidity pre-filter
    assert "official changelog" in m.resolution_criteria
    assert m.fee_params["taker_rate"] == 0.0

    # the tiny market is kept but got no detail call (rate-limit respect)
    assert markets[1].resolution_criteria == ""
    detail_calls = [u for u, _ in api.calls if "/v0/market/" in u]
    assert detail_calls == [f"{connectors.MANIFOLD_BASE}/market/mf1abc"]


def test_manifold_resolutions(api):
    yes = fx("manifold_resolved.json")
    no = fx("manifold_resolved.json"); no["resolution"] = "NO"
    mkt = fx("manifold_resolved.json")
    mkt["resolution"] = "MKT"; mkt["resolutionProbability"] = 0.8
    cancel = fx("manifold_resolved.json"); cancel["resolution"] = "CANCEL"
    still_open = fx("manifold_resolved.json"); still_open["isResolved"] = False
    for name, payload in [("mfyes", yes), ("mfno", no), ("mfmkt", mkt),
                          ("mfcancel", cancel), ("mfopen", still_open)]:
        api.add(f"/market/{name}", payload)

    res = connectors.get_resolution("manifold", "mfyes")
    assert res["outcome"] == 1 and res["disputed"] is False
    assert res["resolved_at"].startswith("2025-07-")  # ms epoch converted

    assert connectors.get_resolution("manifold", "mfno")["outcome"] == 0

    res = connectors.get_resolution("manifold", "mfmkt")
    assert res == {"outcome": 1, "resolved_at": res["resolved_at"],
                   "disputed": True, "note": "resolved MKT at p=0.8"}

    res = connectors.get_resolution("manifold", "mfcancel")
    assert res["outcome"] == 0 and res["disputed"] is True and "CANCEL" in res["note"]

    assert connectors.get_resolution("manifold", "mfopen") is None


# --------------------------------------------------------------- Polymarket

def test_polymarket_string_encoded_numerics(api):
    api.add("gamma-api.polymarket.com/markets", fx("polymarket_markets.json"))
    markets = connectors.list_markets("polymarket")

    _, params = api.calls[0]
    assert params["closed"] == "false" and params["limit"] == 100
    assert params["order"] == "id" and params["ascending"] == "true"

    # non-binary Up/Down market skipped; malformed-prices market survives
    assert [m.market_id for m in markets] == ["500012", "500014"]

    m = markets[0]
    assert m.volume_24h == pytest.approx(15234.5)  # "15234.5" decoded
    assert m.open_interest == pytest.approx(88210.0)
    assert m.book.best_bid == pytest.approx(0.71)
    assert m.book.best_ask == pytest.approx(0.74)
    assert m.category == "Politics"
    assert "Congressional Record" in m.resolution_criteria
    assert json.loads(m.raw["clobTokenIds"])[0].startswith("71321045679")

    m2 = markets[1]  # outcomePrices "not-json" must not raise
    assert m2.book.best_bid == pytest.approx(0.10)  # string bid/ask decoded
    assert m2.book.best_ask == pytest.approx(0.13)


def test_polymarket_clob_book(api):
    api.add("clob.polymarket.com/book", fx("clob_book.json"))
    market = Market(venue="polymarket", market_id="500012", question="q",
                    resolution_criteria="r",
                    raw=fx("polymarket_markets.json")[0])
    book = connectors.get_book(market)

    _, params = api.calls[0]
    assert params["token_id"].startswith("71321045679")  # first = YES token
    assert book.bids == [(0.71, 120.0), (0.70, 50.0)]  # sorted best-first
    assert book.asks == [(0.74, 90.0), (0.75, 40.0)]


def test_polymarket_resolutions(api):
    clean = fx("polymarket_closed.json")
    disputed = fx("polymarket_closed.json")
    disputed["umaResolutionStatuses"] = "[\"disputed\"]"
    still_open = fx("polymarket_closed.json"); still_open["closed"] = False
    api.add("/markets/500020", clean)
    api.add("/markets/500021", disputed)
    api.add("/markets/500022", still_open)

    res = connectors.get_resolution("polymarket", "500020")
    assert res["outcome"] == 0  # yes price "0" => resolved No
    assert res["disputed"] is False and res["resolved_at"].startswith("2026-07-02")

    res = connectors.get_resolution("polymarket", "500021")
    assert res["disputed"] is True and "disputed" in res["note"]

    assert connectors.get_resolution("polymarket", "500022") is None


def test_unknown_venue_raises():
    with pytest.raises(ValueError):
        connectors.list_markets("predictit")
    with pytest.raises(ValueError):
        connectors.get_resolution("predictit", "X")
    with pytest.raises(ValueError):
        connectors.get_book(Market(venue="predictit", market_id="X",
                                   question="q", resolution_criteria="r"))


# ---------------------------------------------------------------- raw cache

class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


class FakeSession:
    def __init__(self, resps):
        self.resps = list(resps)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        assert timeout is not None  # every request must carry a timeout
        self.calls.append((url, params))
        return self.resps.pop(0)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(connectors.time, "sleep", lambda s: None)


def test_cached_get_write_read_roundtrip(tmp_path, monkeypatch, no_sleep):
    payload = {"markets": [{"ticker": "T1"}], "cursor": ""}
    session = FakeSession([FakeResp(200, payload), FakeResp(200, payload)])
    monkeypatch.setattr(connectors, "_session", session)
    monkeypatch.setattr(connectors, "RAW_DIR", tmp_path / "raw")

    out = connectors.cached_get(f"{connectors.KALSHI_BASE}/markets", {"limit": 5})
    assert out == payload

    files = sorted((tmp_path / "raw").rglob("*.json.gz"))
    assert len(files) == 1
    assert files[0].parts[-3] == "kalshi"  # data/raw/{venue}/{YYYY-MM-DD}/...
    assert len(files[0].parts[-2]) == 10  # date directory
    assert connectors.load_raw(files[0]) == payload

    # append-only: a second identical call adds a second file, replaces nothing
    connectors.cached_get(f"{connectors.KALSHI_BASE}/markets", {"limit": 5})
    assert len(list((tmp_path / "raw").rglob("*.json.gz"))) == 2


def test_cached_get_retries_on_429_then_succeeds(tmp_path, monkeypatch, no_sleep):
    session = FakeSession([FakeResp(429, {}), FakeResp(200, {"ok": 1})])
    monkeypatch.setattr(connectors, "_session", session)
    monkeypatch.setattr(connectors, "RAW_DIR", tmp_path / "raw")

    assert connectors.cached_get(f"{connectors.KALSHI_BASE}/markets") == {"ok": 1}
    assert len(session.calls) == 2


def test_cached_get_gives_up_after_retries(tmp_path, monkeypatch, no_sleep):
    session = FakeSession([FakeResp(500, {})] * 4)
    monkeypatch.setattr(connectors, "_session", session)
    monkeypatch.setattr(connectors, "RAW_DIR", tmp_path / "raw")

    with pytest.raises(requests.HTTPError):
        connectors.cached_get(f"{connectors.KALSHI_BASE}/markets")
    assert len(session.calls) == 4  # 1 try + 3 retries


# -------------------------------------------------------- live smoke tests

LIVE = os.environ.get("RUN_LIVE")


@pytest.mark.skipif(not LIVE, reason="set RUN_LIVE=1 for live venue smoke tests")
def test_live_kalshi_list_markets():
    markets = connectors.list_markets("kalshi", limit=5)
    assert 1 <= len(markets) <= 5
    for m in markets:
        assert m.venue == "kalshi" and m.question
        assert m.resolution_criteria
        assert m.fee_params.get("model") == "kalshi_quadratic"


@pytest.mark.skipif(not LIVE, reason="set RUN_LIVE=1 for live venue smoke tests")
def test_live_kalshi_orderbook():
    markets = connectors.list_markets("kalshi", limit=5)
    book = connectors.get_book(markets[0])
    assert isinstance(book, Book) and book.ts
    for price, qty in book.bids + book.asks:
        assert 0.0 <= price <= 1.0 and qty >= 0
