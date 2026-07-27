"""Baselines (doc §2.9): cheap insurance against fooling ourselves.

- market_baseline: p = q (trivially trust the price). Caller logs it as
  baseline_market_p; it never reaches the forecaster.
- no_research_baseline: one SDK query() to the first ensemble model with
  retrieval disabled (allowed_tools=[]) — same prompt, same output contract,
  same cap — so it differs from the harness in exactly one respect.
"""
from __future__ import annotations

import logging

from bot import forecast as _f
from bot.models import Market

log = logging.getLogger("bot.baselines")


def market_baseline(market: Market) -> float | None:
    """Book mid, or None when the book is missing/one-sided."""
    book = getattr(market, "book", None)
    return book.mid if book is not None else None


async def no_research_baseline(market: Market, cfg: dict) -> tuple[float | None, float]:
    """(p, cost_usd) from a single no-retrieval call; (None, 0.0) if the SDK
    is unavailable or the call fails in any way."""
    if _f._query is None:
        return None, 0.0
    cost = 0.0
    try:
        prompt = _f.render_prompt(market)
        if not _f._supports_output_format():
            prompt = prompt + "\n\n" + _f.STRICT_JSON_NOTE
        options = _f._build_options(cfg["models"][0], cfg, allowed_tools=[])
        async for msg in _f._query(prompt=prompt, options=options):
            if not _f._is_result_message(msg):
                continue
            cost = getattr(msg, "total_cost_usd", None) or 0.0
            fc = _f._extract_forecast(msg)
            if fc is None:
                log.warning("no-research baseline: format failure (subtype=%s)",
                            getattr(msg, "subtype", "?"))
                return None, cost
            lo, hi = cfg.get("cap", (0.03, 0.97))
            return min(hi, max(lo, fc.p)), cost
    except Exception as e:
        log.warning("no-research baseline failed: %s: %s", type(e).__name__, e)
    return None, cost
