"""Tests for the blind forecaster (bot/forecast.py) and baselines
(bot/baselines.py). NO live SDK/LLM calls: the module-level `_query` seam is
monkeypatched with a fake async generator. Sync tests + asyncio.run() only
(no pytest-asyncio dependency)."""
from __future__ import annotations

import asyncio
import json

import pytest

from bot import baselines, forecast as fc
from bot.models import Book, Market

CFG = {
    "models": ["opus", "opus", "sonnet"],
    "other_models": [],
    "cap": [0.03, 0.97],
    "max_turns": 5,
    "max_budget_usd": 0.10,
    "trim": 1,
}

MKT = Market(
    venue="kalshi", market_id="TEST-FCC-1",
    question="Will the FCC publish the final order in docket 24-120 by Aug 31, 2026?",
    resolution_criteria=("Resolves YES if the FCC posts the final order in "
                         "docket 24-120 on fcc.gov by 2026-08-31 23:59 ET."),
    resolve_by="2026-08-31T23:59:00-04:00",
    book=Book(bids=[(0.62, 100.0)], asks=[(0.66, 50.0)], ts="2026-07-27T00:00:00Z"),
    volume_24h=1234.0, open_interest=9876.0,
)


# --- blocklist / PreToolUse hook ---------------------------------------------

def _run_pre(query: str | None = None, url: str | None = None):
    hook = fc.make_pre_hook()
    ti: dict = {}
    if query is not None:
        ti["query"] = query
    if url is not None:
        ti["url"] = url
    return asyncio.run(hook({"tool_name": "WebSearch", "tool_input": ti},
                            "tu-1", None))


def test_blocklist_denies_kalshi_url():
    out = _run_pre(url="https://kalshi.com/markets/KXFED")
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == "price-blindness blocklist"


def test_blocklist_denies_odds_query():
    out = _run_pre(query="election odds 2026 senate")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_blocklist_allows_sec_gov():
    assert _run_pre(url="https://www.sec.gov/cgi-bin/browse-edgar") == {}
    assert _run_pre(query="FCC docket 24-120 final order status") == {}


def test_blocked_pure_function():
    assert fc.blocked("polymarket.com/event/x")
    assert fc.blocked("what do Prediction Market prices say")  # case-insensitive
    assert not fc.blocked("https://www.federalregister.gov/documents")


# --- contamination regex ------------------------------------------------------

def test_contamination_fires_on_price_in_market_context():
    assert fc.scan_contamination("The contract is trading at 34¢ on the market.")
    assert fc.scan_contamination("bettors put the odds at 62% this week")


def test_contamination_ignores_plain_percentage():
    assert not fc.scan_contamination("A new poll shows 34% of voters approve.")
    assert not fc.scan_contamination("Turnout rose 5% versus the prior cycle.")


def test_post_hook_counts_retrievals_and_flags_contamination():
    state = fc.MemberState(model="opus")
    hook = fc.make_post_hook(state)
    resp = {"content": [{"type": "text",
                         "text": "the contract is trading at 34¢ on the market"}]}
    asyncio.run(hook({"tool_name": "WebFetch", "tool_response": resp}, "tu", None))
    assert state.retrievals == 1
    assert state.contaminated
    # empty response: no retrieval credit
    state2 = fc.MemberState(model="opus")
    asyncio.run(fc.make_post_hook(state2)({"tool_response": ""}, "tu", None))
    assert state2.retrievals == 0 and not state2.contaminated


# --- aggregation ----------------------------------------------------------------

def test_aggregate_trimmed_mean():
    assert fc.aggregate([0.1, 0.2, 0.3, 0.9], trim=1) == pytest.approx(0.25)


def test_aggregate_plain_mean_when_too_few():
    assert fc.aggregate([0.2, 0.4], trim=1) == pytest.approx(0.3)
    assert fc.aggregate([0.5], trim=1) == pytest.approx(0.5)


def test_aggregate_caps_both_tails():
    assert fc.aggregate([0.001, 0.002, 0.003], trim=1) == pytest.approx(0.03)
    assert fc.aggregate([0.99, 0.995, 1.0], trim=1) == pytest.approx(0.97)


def test_aggregate_empty_raises():
    with pytest.raises(ValueError):
        fc.aggregate([])


# --- prompt renderer (price-blind) ---------------------------------------------

def test_prompt_includes_criteria_and_excludes_prices():
    p = fc.render_prompt(MKT, today="2026-07-27")
    assert MKT.question in p
    assert MKT.resolution_criteria in p
    assert "2026-07-27" in p
    # book is populated but no price/volume field may leak
    assert "0.62" not in p and "0.66" not in p and "0.64" not in p
    assert "1234" not in p and "9876" not in p
    low = p.lower()
    assert "volume" not in low and "book" not in low and "open interest" not in low


# --- fakes for end-to-end tests --------------------------------------------------

class FakeResult:
    def __init__(self, p, subtype="success", cost=0.01, structured=True):
        payload = {"p": p, "rationale": f"rationale p={p}",
                   "sources": [f"https://www.fcc.gov/doc/{p}"],
                   "criteria_interp": f"interp p={p}"}
        self.subtype = subtype
        self.total_cost_usd = cost
        self.structured_output = payload if structured else None
        self.result = json.dumps(payload)


def make_fake_query(specs, drive_hooks=False, payload="FCC posted a draft order."):
    """Fake claude_agent_sdk.query. Each call consumes the next spec:
    float -> member returns that p; "error" -> raises (model-access);
    "badformat" -> unparseable output; "text" -> valid JSON in text only."""
    idx = {"n": 0}

    def fake_query(prompt=None, options=None, **kwargs):
        spec = specs[idx["n"] % len(specs)]
        idx["n"] += 1

        async def gen():
            if drive_hooks and getattr(options, "hooks", None):
                for matcher in options.hooks.get("PostToolUse", []):
                    for h in matcher.hooks:
                        await h({"tool_name": "WebSearch",
                                 "tool_input": {"query": "fcc docket 24-120"},
                                 "tool_response": {"content": [
                                     {"type": "text", "text": payload}]}},
                                "tu-fake", None)
            if spec == "error":
                raise RuntimeError("model not available: access denied")
            if spec == "badformat":
                r = FakeResult(0.5)
                r.structured_output = {"nope": 1}
                r.result = "no json here"
                yield r
                return
            if spec == "text":
                r = FakeResult(0.7)
                r.structured_output = None  # forces defensive text parsing
                yield r
                return
            yield FakeResult(spec)
        return gen()

    return fake_query


# --- end-to-end forecast() --------------------------------------------------------

def test_forecast_e2e_trimmed_mean_and_health(monkeypatch):
    monkeypatch.setattr(fc, "_query", make_fake_query([0.6, 0.7, 0.8],
                                                      drive_hooks=True))
    r = asyncio.run(fc.forecast(MKT, CFG))
    assert r.p == pytest.approx(0.7)  # trim=1 on 3 members -> median
    assert sorted(r.ensemble_members) == [0.6, 0.7, 0.8]
    assert r.ensemble_spread == pytest.approx(0.2)
    assert r.retrieval_ok is True
    assert r.contaminated is False
    assert r.cost_usd == pytest.approx(0.03)
    assert r.rationale and r.criteria_interp
    assert any("fcc.gov" in s for s in r.sources)


def test_forecast_drops_failed_member(monkeypatch):
    monkeypatch.setattr(fc, "_query", make_fake_query([0.6, "error", 0.8],
                                                      drive_hooks=True))
    r = asyncio.run(fc.forecast(MKT, CFG))
    assert len(r.ensemble_members) == 2       # errored member dropped
    assert r.p == pytest.approx(0.7)          # plain mean (too few to trim)
    assert r.retrieval_ok is True


def test_forecast_fewer_than_two_survivors(monkeypatch):
    monkeypatch.setattr(fc, "_query",
                        make_fake_query(["error", "badformat", 0.6],
                                        drive_hooks=True))
    r = asyncio.run(fc.forecast(MKT, CFG))
    assert r.ensemble_members == [0.6]
    assert r.p == pytest.approx(0.6)
    assert r.retrieval_ok is False


def test_forecast_all_members_fail(monkeypatch):
    monkeypatch.setattr(fc, "_query",
                        make_fake_query(["error", "error", "badformat"]))
    r = asyncio.run(fc.forecast(MKT, CFG))
    assert r.retrieval_ok is False
    assert r.ensemble_members == []
    assert r.p == pytest.approx(0.5)


def test_forecast_thin_retrieval_majority(monkeypatch):
    # hooks never driven -> zero retrievals per member -> majority thin
    monkeypatch.setattr(fc, "_query", make_fake_query([0.6, 0.7, 0.8]))
    r = asyncio.run(fc.forecast(MKT, CFG))
    assert r.p == pytest.approx(0.7)
    assert r.retrieval_ok is False


def test_forecast_contamination_flag(monkeypatch):
    monkeypatch.setattr(
        fc, "_query",
        make_fake_query([0.6, 0.7, 0.8], drive_hooks=True,
                        payload="the yes contract is trading at 34¢ on the market"))
    r = asyncio.run(fc.forecast(MKT, CFG))
    assert r.contaminated is True


def test_forecast_caps_output(monkeypatch):
    monkeypatch.setattr(fc, "_query",
                        make_fake_query([0.005, 0.004, 0.006], drive_hooks=True))
    r = asyncio.run(fc.forecast(MKT, CFG))
    assert r.p == pytest.approx(0.03)


def test_forecast_parses_text_fallback(monkeypatch):
    monkeypatch.setattr(fc, "_query",
                        make_fake_query(["text", "text", "text"], drive_hooks=True))
    r = asyncio.run(fc.forecast(MKT, CFG))
    assert r.p == pytest.approx(0.7)


def test_forecast_other_models_stub_dropped(monkeypatch):
    monkeypatch.setattr(fc, "_query", make_fake_query([0.6, 0.7, 0.8],
                                                      drive_hooks=True))
    cfg = dict(CFG, other_models=["gpt-x"])
    r = asyncio.run(fc.forecast(MKT, cfg))  # NotImplementedError seam -> dropped
    assert len(r.ensemble_members) == 3
    assert r.p == pytest.approx(0.7)


def test_forecast_raises_without_sdk(monkeypatch):
    monkeypatch.setattr(fc, "_query", None)
    with pytest.raises(RuntimeError):
        asyncio.run(fc.forecast(MKT, CFG))


def test_forecast_sync_wrapper(monkeypatch):
    monkeypatch.setattr(fc, "_query", make_fake_query([0.6, 0.7, 0.8],
                                                      drive_hooks=True))
    r = fc.forecast_sync(MKT, CFG)
    assert r.p == pytest.approx(0.7)


# --- pure-logic importability (no SDK required) ------------------------------------

def test_pure_helpers_do_not_need_sdk():
    # These must be plain module-level callables usable even if the SDK import
    # failed (fc.ClaudeAgentOptions may be None in that case).
    assert callable(fc.blocked) and callable(fc.scan_contamination)
    assert callable(fc.render_prompt) and callable(fc.aggregate)
    assert callable(fc.parse_member_json) and callable(fc.make_pre_hook)
    mf = fc.parse_member_json(
        'noise {"p": 0.4, "rationale": "r", "sources": [], "criteria_interp": ""} tail')
    assert mf is not None and mf.p == pytest.approx(0.4)
    assert fc.parse_member_json("no json at all") is None


# --- baselines -----------------------------------------------------------------------

def test_market_baseline_mid_and_none():
    assert baselines.market_baseline(MKT) == pytest.approx(0.64)
    no_book = Market(venue="kalshi", market_id="X", question="q",
                     resolution_criteria="c")
    assert baselines.market_baseline(no_book) is None
    one_sided = Market(venue="kalshi", market_id="Y", question="q",
                       resolution_criteria="c", book=Book(bids=[(0.5, 1.0)]))
    assert baselines.market_baseline(one_sided) is None


def test_no_research_baseline(monkeypatch):
    monkeypatch.setattr(fc, "_query", make_fake_query([0.99]))
    p, cost = asyncio.run(baselines.no_research_baseline(MKT, CFG))
    assert p == pytest.approx(0.97)  # same cap as the harness
    assert cost == pytest.approx(0.01)


def test_no_research_baseline_failure_paths(monkeypatch):
    monkeypatch.setattr(fc, "_query", None)
    assert asyncio.run(baselines.no_research_baseline(MKT, CFG)) == (None, 0.0)
    monkeypatch.setattr(fc, "_query", make_fake_query(["error"]))
    assert asyncio.run(baselines.no_research_baseline(MKT, CFG)) == (None, 0.0)
    monkeypatch.setattr(fc, "_query", make_fake_query(["badformat"]))
    p, _ = asyncio.run(baselines.no_research_baseline(MKT, CFG))
    assert p is None
