"""Orchestrator: settle -> ingest -> select -> forecast(BLIND) -> decide -> sim -> log.

Semi-interactive runner (doc §2.10). The frozen config.yaml is the
pre-registration; this module only executes it.

  python -m bot.run                  # full pipeline run
  python -m bot.run --baseline-only  # Phase-0: log market-baseline rows, no harness
  python -m bot.run --settle-only    # just close the loop
  python -m bot.run --report         # print evaluator summary
  python -m bot.run --limit 200      # cap ingested markets (cost/time control)
  python -m bot.run --fast           # skip the staleness re-fetch for maker fills

Budget discipline: forecasting stops loudly once run.max_run_cost_usd is hit.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import traceback
from pathlib import Path

import yaml

from bot import baselines, connectors, decide as decide_mod, evaluate, execute
from bot import forecast as forecast_mod, log, select, settle
from bot.models import FillResult

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | Path = ROOT / "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _snap(book) -> dict:
    if book is None:
        return {}
    return {"q_mid": book.mid, "bid": book.best_bid, "ask": book.best_ask}


def _process_market(m, cfg, con, portfolio, baseline_only: bool) -> dict | None:
    """Forecast + decide one market; returns a pending log row (no maker fill yet)."""
    hv = "p0-baseline" if baseline_only else cfg["harness_version"]
    if log.already_forecast(con, m.venue, m.market_id, hv):
        return None

    snap = _snap(m.book)  # listing-time snapshot
    try:
        fresh = connectors.get_book(m)  # fresh decision-time book
        if fresh.mid is not None:
            m.book = fresh
        # else: empty/one-sided live book (thin tail) — keep the listing
        # snapshot so baseline q exists; decide() still guards depth.
    except Exception:
        pass  # keep the listing book; decide() guards one-sided/missing books
    dec_snap = _snap(m.book)
    baseline_p = baselines.market_baseline(m)

    if baseline_only:
        fr, nr_p, cost = None, None, 0.0
        p, d = baseline_p, decide_mod.DecisionResult(side="pass", reason="baseline-only run")
        if p is None:
            return None
    else:
        fr = forecast_mod.forecast_sync(m, cfg["forecaster"])
        nr_p, nr_cost = asyncio.run(baselines.no_research_baseline(m, cfg["forecaster"]))
        cost = fr.cost_usd + nr_cost
        p = fr.p
        if fr.contaminated or not fr.retrieval_ok:
            d = decide_mod.DecisionResult(
                side="pass",
                reason="contaminated forecast" if fr.contaminated else "thin retrieval")
        else:
            d = decide_mod.decide(p, m, cfg["decision"], portfolio)

    fill = FillResult(price=None)
    if d.side != "pass" and d.order_type == "taker" and m.book is not None:
        fill = execute.sim_fill_taker(m.book, d.side, d.stake, m.fee_params,
                                      cfg["execution"])

    return {
        "market": m, "row": dict(
            venue=m.venue, market_id=m.market_id, question=m.question,
            category=m.category, vertical=cfg["vertical"],
            cluster_id=m.cluster_id or select.assign_cluster(m),
            p=p, q_mid_snap=snap.get("q_mid"), bid_snap=snap.get("bid"),
            ask_snap=snap.get("ask"), q_mid_decide=dec_snap.get("q_mid"),
            bid_decide=dec_snap.get("bid"), ask_decide=dec_snap.get("ask"),
            edge=d.edge, side=d.side, order_type=d.order_type,
            order_price=d.order_price, stake=d.stake,
            fill_price=fill.price, fill_qty_frac=fill.qty_frac, fee=fill.fee,
            fee_params=m.fee_params, harness_version=hv,
            decision_version=cfg["decision_version"],
            rationale=(fr.rationale if fr else d.reason),
            sources=(fr.sources if fr else []),
            criteria_interp=(fr.criteria_interp if fr else ""),
            ensemble_members=(fr.ensemble_members if fr else []),
            ensemble_spread=(fr.ensemble_spread if fr else None),
            retrieval_ok=(int(fr.retrieval_ok) if fr else None),
            contaminated=(int(fr.contaminated) if fr else None),
            baseline_market_p=baseline_p, baseline_norsrch_p=nr_p,
            cost_usd=cost, resolve_by=m.resolve_by or m.close_time,
        ),
        "cost": cost, "maker": d.side != "pass" and d.order_type == "maker",
    }


def run_pipeline(cfg: dict, limit: int | None = None, baseline_only: bool = False,
                 fast: bool = False, con=None) -> dict:
    con = con or log.connect(cfg["run"]["db_path"])
    run_id = log.start_run(con)
    try:
        settled = settle.settle_open(con, cfg)
        markets = connectors.list_markets(cfg["venue"], limit=limit)
        shortlist, funnel = select.run_funnel(markets, cfg["selection"])

        portfolio = decide_mod.PortfolioState()
        pending, total_cost, budget_hit = [], 0.0, False
        for m in shortlist:
            if total_cost >= cfg["run"]["max_run_cost_usd"]:
                budget_hit = True
                break
            out = _process_market(m, cfg, con, portfolio, baseline_only)
            if out:
                pending.append(out)
                total_cost += out["cost"]

        # Maker fills need a later book (trade-through estimate): one staleness
        # wait for the whole batch, then re-fetch each maker market's book.
        maker_rows = [o for o in pending if o["maker"]]
        if maker_rows and not fast:
            time.sleep(min(cfg["execution"].get("staleness_seconds", 60), 60))
            for o in maker_rows:
                try:
                    later = connectors.get_book(o["market"])
                    f = execute.estimate_maker_fill(
                        o["row"]["order_price"], o["row"]["side"], later,
                        cfg["execution"])
                    o["row"].update(fill_price=f.price, fill_qty_frac=f.qty_frac,
                                    fee=f.fee)
                except Exception:
                    pass  # stays unfilled — the conservative outcome

        for o in pending:
            log.insert_forecast(con, **o["row"])

        n_traded = sum(1 for o in pending if o["row"]["side"] != "pass")
        summary = dict(status="budget-halt" if budget_hit else "ok",
                       settled=settled, funnel=funnel,
                       n_ingested=len(markets), n_selected=len(shortlist),
                       n_forecast=len(pending), n_traded=n_traded,
                       cost_usd=round(total_cost, 4))
        log.finish_run(con, run_id, "error" if budget_hit else "ok",
                       stage="done", n_ingested=len(markets),
                       n_selected=len(shortlist), n_forecast=len(pending),
                       n_traded=n_traded, cost_usd=total_cost, funnel=funnel,
                       error="max_run_cost_usd hit" if budget_hit else None)
        return summary
    except Exception:
        log.finish_run(con, run_id, "error", error=traceback.format_exc())
        raise


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--baseline-only", action="store_true")
    ap.add_argument("--settle-only", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--fast", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config()
    con = log.connect(cfg["run"]["db_path"])
    if args.report:
        print(json.dumps(evaluate.report(con), indent=2, default=str))
    elif args.settle_only:
        print(json.dumps(settle.settle_open(con, cfg), indent=2))
    else:
        print(json.dumps(run_pipeline(cfg, limit=args.limit,
                                      baseline_only=args.baseline_only,
                                      fast=args.fast, con=con),
                         indent=2, default=str))


if __name__ == "__main__":
    main()
