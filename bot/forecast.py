"""Blind forecasting harness on the Claude Agent SDK (doc §1 map, §2.3, App. B).

Price-blindness is mechanical, not aspirational:
  - the prompt carries ONLY question + resolution criteria + today's date
    (never any price/book/volume field);
  - a PreToolUse hook denies WebSearch/WebFetch calls touching blocklisted
    venues/odds sites;
  - a PostToolUse hook scans fetched content for price-like mentions and sets
    the per-call contamination flag (-> ForecastResult.contaminated).

Each ensemble member is an isolated `query()` session (no shared context),
run concurrently. Failed members (format failure, model-access error, any
exception) are dropped and logged. Aggregate = trimmed mean of member p's,
capped into cfg["cap"].

All pure logic (blocklist, contamination scan, prompt rendering, JSON
parsing, aggregation, hook factories) lives in module-level functions that
import and run WITHOUT the SDK; only forecast() itself requires it. Tests
monkeypatch the module-level `_query` seam.
"""
from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
import re
import typing
from dataclasses import dataclass, field
from datetime import date

from pydantic import BaseModel, Field

from bot.models import ForecastResult, Market

log = logging.getLogger("bot.forecast")

# --- SDK import (graceful degradation: everything below except forecast()
# --- must keep working when the SDK is not installed) -----------------------
try:
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query as _sdk_query
    try:
        from claude_agent_sdk import PermissionMode as _PermissionMode
    except ImportError:  # older SDKs don't export the literal
        _PermissionMode = None
except ImportError:  # pragma: no cover - exercised only without the SDK
    ClaudeAgentOptions = HookMatcher = _PermissionMode = None
    _sdk_query = None

_query = _sdk_query  # module-level seam; tests monkeypatch this


# --- output contract (doc §2.3) ----------------------------------------------
class MemberForecast(BaseModel):
    """One ensemble member's structured output."""

    p: float = Field(ge=0.0, le=1.0)
    rationale: str
    sources: list[str] = Field(default_factory=list)
    criteria_interp: str = ""


# --- price-blindness blocklist (PreToolUse) ----------------------------------
BLOCKLIST = ("kalshi", "polymarket", "manifold", "electionbettingodds",
             "predictit", "betfair", "metaculus", "smarkets", "odds",
             "prediction market")


def blocked(text: str) -> bool:
    """True if a search query / URL touches a blocklisted substring."""
    t = (text or "").lower()
    return any(b in t for b in BLOCKLIST)


# --- contamination scan (PostToolUse) ----------------------------------------
# Price-like token: 1-2 digits followed by % / cent-sign / "cents", ...
_PRICE_RE = re.compile(r"\b\d{1,2}\s?(?:%|¢|cents?\b)")
# ... co-occurring (within a window) with betting/odds/market context words.
_BET_CONTEXT_RE = re.compile(
    r"\b(?:bet|bets|betting|bettors?|odds|wagers?|bookmakers?|"
    r"trading|traded|trades?|priced|prices?|market|markets|"
    r"contracts?|shares?)\b", re.IGNORECASE)
_CONTEXT_WINDOW = 100  # chars either side of the price token


def scan_contamination(text: str) -> bool:
    """True if text mentions a price-like figure in betting/market context."""
    text = text or ""
    for m in _PRICE_RE.finditer(text):
        lo = max(0, m.start() - _CONTEXT_WINDOW)
        if _BET_CONTEXT_RE.search(text[lo:m.end() + _CONTEXT_WINDOW]):
            return True
    return False


# --- per-member state + hook factories (pure: no SDK types needed) -----------
@dataclass
class MemberState:
    """Per-member retrieval/contamination bookkeeping, mutated by hooks."""

    model: str = ""
    retrievals: int = 0       # successful WebSearch/WebFetch results (§2.3 health)
    contaminated: bool = False


_DENY = {"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "price-blindness blocklist"}}


def make_pre_hook(state: MemberState | None = None):
    """PreToolUse hook on WebSearch|WebFetch: mechanical price-blindness."""
    async def pre_hook(input_data, tool_use_id, context):
        ti = (input_data or {}).get("tool_input") or {}
        text = " ".join(str(ti.get(k) or "") for k in ("query", "url", "prompt"))
        if blocked(text):
            log.info("blocked research call (member %s): %.120s",
                     state.model if state else "?", text)
            return _DENY
        return {}
    return pre_hook


def _response_text(resp) -> str:
    """Defensively flatten a tool_response of unknown shape into text."""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        return " ".join(_response_text(v) for v in resp.values())
    if isinstance(resp, (list, tuple)):
        return " ".join(_response_text(v) for v in resp)
    return str(resp)


def make_post_hook(state: MemberState):
    """PostToolUse hook: count successful retrievals; scan for price leaks."""
    async def post_hook(input_data, tool_use_id, context):
        text = _response_text((input_data or {}).get("tool_response"))
        if text.strip():
            state.retrievals += 1
        if scan_contamination(text):
            state.contaminated = True
            log.warning("contamination hit (member %s, tool_use %s)",
                        state.model, tool_use_id)
        return {}
    return post_hook


# --- prompt (blind: question + criteria + date; NEVER price/book/volume) -----
SYSTEM_PROMPT = """\
You are a rigorous forecaster producing one calibrated probability for a \
binary prediction question. Work strictly in this order:

(a) RESOLUTION-CRITERIA INTERPRETATION FIRST. Restate the resolution rule in
    your own words. Name the authoritative source that decides it. Enumerate
    edge cases: timezone of the deadline, "by" vs "before", what a
    technicality would do, and the default outcome if nothing changes by the
    deadline. Check whether the question is already effectively resolved.
(b) OUTSIDE VIEW. State an explicit base rate for this class of event and
    where it comes from.
(c) INSIDE VIEW from retrieved PRIMARY sources: statutes, dockets, filings,
    official calendars, schedules, changelogs — not news hot takes.
(d) Weigh the strongest reasons YES against the strongest reasons NO.
(e) Final probability p, consistent with (a)-(d).

Rules: never output 0 or 1. You may NOT access betting or prediction-market
sites or search for market prices or odds; forecast from the evidence alone.
Cite the sources you actually used (URLs) in `sources`. Put your (a) analysis
in `criteria_interp` and your (b)-(d) reasoning in `rationale`.\
"""

STRICT_JSON_NOTE = """\
Respond with ONLY a JSON object, no other text, exactly these keys:
{"p": <float in (0,1)>, "rationale": <string>, "sources": [<url strings>],
 "criteria_interp": <string>}\
"""


def render_prompt(market: Market, today: str | None = None) -> str:
    """The member prompt. Question + criteria + date only — never q/book/volume."""
    parts = [
        f"Today's date: {today or date.today().isoformat()}",
        f"Question: {market.question}",
        f"Resolution criteria:\n{market.resolution_criteria}",
    ]
    if market.resolve_by:
        parts.append(f"Resolution deadline (resolve_by): {market.resolve_by}")
    parts.append("Follow your forecasting procedure and produce the final "
                 "forecast for this question.")
    return "\n\n".join(parts)


# --- defensive JSON parsing (fallback when structured output unavailable) ----
def parse_member_json(text: str) -> MemberForecast | None:
    """Best-effort parse of a member's textual answer into MemberForecast."""
    for cand in _json_candidates(text or ""):
        try:
            return MemberForecast.model_validate(cand)
        except Exception:
            continue
    return None


def _json_candidates(text: str):
    try:
        yield json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)  # first { ... last }
    if m:
        try:
            yield json.loads(m.group(0))
        except Exception:
            pass


# --- aggregation (doc §2.3: trimmed mean + cap) -------------------------------
def aggregate(ps: list[float], trim: int = 1,
              cap: tuple[float, float] = (0.03, 0.97)) -> float:
    """Trimmed mean of member p's (plain mean if too few), capped into cap."""
    if not ps:
        raise ValueError("aggregate() needs at least one member p")
    ps = sorted(ps)
    if trim > 0 and len(ps) > 2 * trim:
        ps = ps[trim:-trim]
    lo, hi = cap
    return min(hi, max(lo, sum(ps) / len(ps)))


# --- SDK feature detection ----------------------------------------------------
def _option_fields() -> set[str]:
    if ClaudeAgentOptions is None:
        return set()
    try:
        return {f.name for f in dataclasses.fields(ClaudeAgentOptions)}
    except TypeError:
        return set(inspect.signature(ClaudeAgentOptions).parameters)


def _supports_output_format() -> bool:
    return "output_format" in _option_fields()


def _permission_mode() -> str:
    """'dontAsk' if the installed SDK accepts it, else 'bypassPermissions'."""
    try:
        modes = typing.get_args(_PermissionMode)
        if modes and "dontAsk" not in modes:
            return "bypassPermissions"
    except Exception:
        pass
    return "dontAsk"


def _build_options(model: str, cfg: dict, state: MemberState | None = None,
                   allowed_tools: list[str] | None = None):
    """ClaudeAgentOptions for one member; feature-detects optional fields."""
    tools = ["WebSearch", "WebFetch"] if allowed_tools is None else allowed_tools
    fields = _option_fields()
    kw: dict = dict(model=model, system_prompt=SYSTEM_PROMPT,
                    allowed_tools=tools, permission_mode=_permission_mode(),
                    max_turns=cfg.get("max_turns", 25))
    if "max_budget_usd" in fields and cfg.get("max_budget_usd"):
        kw["max_budget_usd"] = cfg["max_budget_usd"]
    if _supports_output_format():
        kw["output_format"] = {"type": "json_schema",
                               "schema": MemberForecast.model_json_schema()}
    if state is not None and tools:
        kw["hooks"] = {
            "PreToolUse": [HookMatcher(matcher="WebSearch|WebFetch",
                                       hooks=[make_pre_hook(state)])],
            "PostToolUse": [HookMatcher(matcher="WebSearch|WebFetch",
                                        hooks=[make_post_hook(state)])],
        }
    return ClaudeAgentOptions(**{k: v for k, v in kw.items() if k in fields})


# --- members ------------------------------------------------------------------
def _is_result_message(msg) -> bool:
    """Duck-typed ResultMessage check (works with fakes in tests)."""
    return getattr(msg, "subtype", None) is not None and hasattr(msg, "total_cost_usd")


def _extract_forecast(msg) -> MemberForecast | None:
    if getattr(msg, "subtype", "") != "success":
        return None
    so = getattr(msg, "structured_output", None)
    if so is not None:
        try:
            return MemberForecast.model_validate(so)
        except Exception:
            pass  # fall through to text parsing
    return parse_member_json(getattr(msg, "result", None) or "")


async def _run_member(prompt: str, model: str, cfg: dict,
                      state: MemberState) -> tuple[MemberForecast | None, float]:
    """One isolated SDK session. Any failure (model access, format, ...) drops
    the member gracefully: returns (None, cost_so_far)."""
    cost = 0.0
    if not _supports_output_format():
        prompt = prompt + "\n\n" + STRICT_JSON_NOTE
    try:
        options = _build_options(model, cfg, state=state)
        async for msg in _query(prompt=prompt, options=options):
            if not _is_result_message(msg):
                continue
            cost = getattr(msg, "total_cost_usd", None) or 0.0
            fc = _extract_forecast(msg)
            if fc is None:
                log.warning("member %s dropped: format failure (subtype=%s)",
                            model, getattr(msg, "subtype", "?"))
            return fc, cost
    except Exception as e:  # model-access errors, transport errors, ...
        log.warning("member %s dropped: %s: %s", model, type(e).__name__, e)
    return None, cost


def forecast_via_api(prompt: str, model: str) -> MemberForecast:
    """SEAM: non-Claude ensemble member via plain API call (doc §1 map — e.g.
    litellm / forecasting-tools GeneralLlm behind the same contract).
    Not implemented yet; callers drop the member gracefully."""
    raise NotImplementedError("non-Claude ensemble members not wired up yet")


async def _run_other(prompt: str, model: str) -> tuple[MemberForecast | None, float]:
    try:
        return forecast_via_api(prompt, model), 0.0
    except NotImplementedError:
        log.warning("other-model member %s dropped: forecast_via_api is a stub", model)
    except Exception as e:
        log.warning("other-model member %s dropped: %s: %s", model, type(e).__name__, e)
    return None, 0.0


# --- public API ----------------------------------------------------------------
async def forecast(market: Market, cfg: dict) -> ForecastResult:
    """Blind ensemble forecast for one market. cfg = `forecaster` block of
    config.yaml. Raises RuntimeError if the SDK is unavailable."""
    if _query is None:
        raise RuntimeError("claude_agent_sdk is not installed; forecast() "
                           "requires it (pure helpers remain importable)")
    prompt = render_prompt(market)
    models = list(cfg.get("models") or [])
    states = [MemberState(model=m) for m in models]
    tasks = [_run_member(prompt, m, cfg, st) for m, st in zip(models, states)]
    others = list(cfg.get("other_models") or [])
    tasks += [_run_other(prompt, m) for m in others]

    outs = await asyncio.gather(*tasks)
    cost = sum(c for _, c in outs)
    padded_states: list[MemberState | None] = states + [None] * len(others)
    survivors = [(fc, st) for (fc, _), st in zip(outs, padded_states)
                 if fc is not None]
    contaminated = any(st.contaminated for st in states)

    if not survivors:
        log.error("forecast %s: all %d ensemble members failed",
                  market.key, len(tasks))
        return ForecastResult(p=0.5, rationale="all ensemble members failed",
                              retrieval_ok=False, contaminated=contaminated,
                              cost_usd=cost)

    ps = [fc.p for fc, _ in survivors]
    p = aggregate(ps, trim=int(cfg.get("trim", 1)),
                  cap=tuple(cfg.get("cap", (0.03, 0.97))))

    # Retrieval health (§2.3): a surviving member with zero successful
    # WebSearch/WebFetch results is "thin"; majority thin => not ok.
    surv_states = [st for _, st in survivors if st is not None]
    thin = sum(1 for st in surv_states if st.retrievals == 0)
    majority_thin = bool(surv_states) and thin * 2 > len(surv_states)
    retrieval_ok = len(survivors) >= 2 and not majority_thin

    best = min((fc for fc, _ in survivors), key=lambda f: abs(f.p - p))
    sources = list(dict.fromkeys(s for fc, _ in survivors for s in fc.sources))
    return ForecastResult(
        p=p, rationale=best.rationale, sources=sources,
        criteria_interp=best.criteria_interp, ensemble_members=ps,
        ensemble_spread=max(ps) - min(ps) if len(ps) > 1 else 0.0,
        retrieval_ok=retrieval_ok, contaminated=contaminated, cost_usd=cost)


def forecast_sync(market: Market, cfg: dict) -> ForecastResult:
    """Sync wrapper for callers outside an event loop."""
    return asyncio.run(forecast(market, cfg))
