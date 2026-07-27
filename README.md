# Prediction-Market Forecasting Bot

A paper-trading experiment that answers one question honestly: **does an
LLM forecasting harness beat the market's log score, out of sample, in one
niche?** Full design rationale, evidence base, and math:
[docs/prediction-market-bot.md](docs/prediction-market-bot.md).

Not a money printer. The pre-registered deliverable is a Δ_log estimate
with cluster-aware uncertainty — and "no detectable edge" is a valid result.

## Layout

```
bot/
  connectors.py   ingest: Kalshi (primary), Manifold (testbed), Polymarket (read-only)
  select.py       the selection funnel + cluster assignment (where the edge is created)
  forecast.py     blind LLM harness on the Claude Agent SDK (never sees prices)
  baselines.py    market baseline (p=q) and no-research LLM baseline
  decide.py       maker-first decision layer: shrunk beliefs, fees, gates, fractional Kelly
  execute.py      trade-through fill simulator, markouts, paper P&L
  log.py          append-only SQLite log — the scientific artifact
  settle.py       resolution polling with dispute flags
  evaluate.py     Δ_log, cluster bootstrap, permutation, confidence sequences, EB shrinkage
  run.py          orchestrator (reads the frozen config.yaml = pre-registration)
dashboard/app.py  Streamlit observability (8 panels; Δ_log big, P&L small)
config.yaml       THE PRE-REGISTRATION — committing a change bumps a version
data/forecasts.db the log (gitignored); data/raw/ cached API responses (gitignored)
```

## Setup

```bash
pip install -r requirements.txt
python3 -m pytest tests/ -q         # 124 tests, all offline
RUN_LIVE=1 python3 -m pytest tests/test_connectors.py -q   # optional live smoke
```

The forecaster runs on the Claude Agent SDK. Personal automation draws on
your Claude plan via the Claude Code login (leave `ANTHROPIC_API_KEY`
unset); set an API key instead if you want isolation from your
interactive usage caps.

## Running

```bash
python3 -m bot.run --baseline-only --limit 2000   # Phase 0: close the loop, no LLM cost
python3 -m bot.run --limit 2000                   # Phase 1: full run (settle → ingest →
                                                  #   funnel → blind forecast → decide → sim → log)
python3 -m bot.run --settle-only                  # just poll resolutions
python3 -m bot.run --report                       # evaluator summary as JSON
streamlit run dashboard/app.py                    # the dashboard
```

Run cadence is semi-interactive by design (you kick it off). Costs are
capped per ensemble member (`forecaster.max_budget_usd`) and per run
(`run.max_run_cost_usd` — the run halts loudly).

## The rules (from the design doc — do not break these)

1. **Forward only.** Never backtest on resolved markets; the LLM knows.
2. **The forecaster never sees a price.** Enforced by SDK hooks; rows that
   trip the contamination scan are excluded from the headline metric.
3. **Forecast-time log fields are immutable.** Settlement writes outcome
   fields only; markouts are the one other permitted update.
4. **Changes are versioned.** Editing prompts/pipeline → bump
   `harness_version`; thresholds/sizing → bump `decision_version`
   (decision changes may be tuned by replay; forecaster changes need
   fresh forward data or a paired shadow run).
5. **Count clusters, not markets.** The pre-registered decision point is
   `target_n_clusters` resolved clusters at `min_edge_nats`; watch only
   the confidence sequence until then.

## Phase status

- Phase 0 (close the loop on paper): **verified live** — full pipeline runs
  against real Kalshi data end to end (ingest → funnel → baseline log →
  settle-ready rows with cluster IDs).
- Phase 1 (blind harness forward runs): ready — needs Claude auth.
- Phase 2 (dashboard + improvement loops): dashboard built; loops begin
  once resolutions accumulate.
- Phase 3 (tiny real money): only if Phase 2's gate is met. See the doc.
