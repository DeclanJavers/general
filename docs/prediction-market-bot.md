# Prediction-Market Forecasting Bot — Design & Build Doc

> **Thesis in one line:** you are not building a model that predicts outcomes. You are building a *disagreement detector with a track record* — something that identifies where the market price is wrong, in corners where sharp money isn't looking, and proves out-of-sample that it's right when it disagrees.

This document is the full plan: every component, how to build it, how to improve it, how to watch it in a web dashboard, plus a self-contained math appendix and minimal code scaffolds.

---

## Table of contents

0. [Read this first — the core thesis](#0-read-this-first--the-core-thesis)
1. [System overview](#1-system-overview)
2. [Components: build + improve + observe](#2-components-build--improve--observe)
3. [Improvement loops (the meta-process)](#3-improvement-loops-the-meta-process)
4. [Observability — the web dashboard](#4-observability--the-web-dashboard)
5. [Phased roadmap](#5-phased-roadmap)
6. [Pitfalls / anti-patterns checklist](#6-pitfalls--anti-patterns-checklist)
- [Appendix A — the math](#appendix-a--the-math)
- [Appendix B — schema + core code](#appendix-b--schema--core-code)
- [Appendix C — resources](#appendix-c--resources)

---

## 0. Read this first — the core thesis

Every design decision below follows from these. Re-read them whenever a choice feels arbitrary.

1. **The objective is the market's error, not the outcome.** The scalar that matters is the **edge** `e = p − q`, where `p` is your probability and `q` is the market's. A model that reproduces `q` has predicted "zero error everywhere" and is worthless — correctly, it will never bet. Usefulness lives entirely in the gap.
2. **The edge is selection + research + calibration, not foresight.** You win by being disciplined about *where* you compete (thin, obscure, document-resolved markets with no sharp money) far more than by being smart *when* you compete. Most questions you should skip.
3. **The forecaster must be blind to the price.** If the harness sees `q`, it will anchor and echo it, and you will have rebuilt the useless model. Compute `q` separately and only combine it in the decision layer.
4. **The real goal of this project is to find out cheaply whether an edge exists.** Success = a defensible answer to "does my harness beat the market's log score out-of-sample in one niche?" — not a bankroll.
5. **Constraints while paper-trading:**
   - **Forward only.** Never backtest on already-resolved markets: the LLM either saw the outcome in training or can look it up, so it will ace the test for fake reasons. The only clean signal is live forecasts on questions unresolved at forecast time. This costs real calendar time; there is no shortcut.
   - **Score the executable price, not the mid.** Simulate fills against the real order book. Scoring at mid with no fees manufactures a fake edge.
   - **Sample size gate.** Expect ~**100+ independent resolved forecasts** before any positive number is distinguishable from luck (error shrinks like 1/√N).

**Design principles that fall out of the above:**

- *Independence:* forecaster never sees `q`.
- *Append-only truth:* the forecast log is the scientific artifact; forecast-time fields are immutable.
- *Versioning resets the clock:* every change to prompt/pipeline/strategy is a new harness version; you cannot count pre-change forecasts toward the new version's record.
- *Baselines everywhere:* always log a dumb baseline (the price itself; a single no-research LLM call) so you can tell whether the expensive machinery earns its keep.
- *Segment by category and version:* the edge is almost always concentrated. Never report only a global average.

---

## 1. System overview

### Pipeline

```
                 ┌─────────────────── improvement loop ───────────────────┐
                 v                                                         │
  ingest ──> select ──> forecast(BLIND to q) ──> decide(edge+size) ──> sim-execute
   (2.1)     (2.2)          (2.3)                    (2.4)               (2.5)
                                                                          │
                                                                          v
                                                                        LOG (2.6)  <── append-only
                                                                          │
                                                                     [ wait days/months ]
                                                                          │
                                                                          v
                                                                     settle (2.7)
                                                                          │
                                                                          v
                                                                    evaluate (2.8) ──> dashboard (§4)
                                                                          │
                                                                          └──> diagnose ──> change ONE thing ──┘
```

`q` is computed alongside the forecaster (from the same market data) and injected only at `decide`. The forecaster receives the question, resolution criteria, and researched context — never the price.

### Component map

| # | Component | Purpose | Criticality |
|---|-----------|---------|-------------|
| 2.1 | Market connectors | Pull markets, prices, order books, resolution criteria, outcomes | High (foundation) |
| 2.2 | Selection / filter | Funnel universe down to exploitable questions; control cost | **Highest** (this *is* the edge) |
| 2.3 | Forecasting harness | Produce `p` + rationale + sources, blind to `q` | High |
| 2.4 | Decision & sizing | `e = p − q`; trade only if `|e|` clears spread+fee; fractional Kelly | High |
| 2.5 | Paper execution / fill sim | Realistic fills at executable price; paper portfolio | Medium |
| 2.6 | Append-only log | The scientific record everything reads from | **Highest** (easy to under-build) |
| 2.7 | Settlement tracker | Close the loop; record outcomes; flag ambiguity | High |
| 2.8 | Evaluation engine | Brier, log, calibration, market delta, by category+version | High |
| 2.9 | Baselines | Prove the harness beats "do nothing / no research" | Medium (cheap insurance) |
| 2.10 | Orchestration + pre-registration | Run on a cadence; freeze the spec; version discipline | Medium |

### Suggested repo layout (minimal)

```
bot/
  connectors.py     # 2.1  fetch markets, books, resolutions
  select.py         # 2.2  the funnel
  forecast.py       # 2.3  the harness (blind)
  decide.py         # 2.4  edge + sizing
  execute.py        # 2.5  fill simulation, paper portfolio
  log.py            # 2.6  append-only writes over SQLite
  settle.py         # 2.7  poll resolutions
  evaluate.py       # 2.8  scoring functions (Appendix B)
  baselines.py      # 2.9
  run.py            # 2.10 orchestration; reads a frozen config.yaml (the pre-registration)
  config.yaml       # pinned harness version, vertical, thresholds, kelly fraction
dashboard/          # §4  web app reading the SQLite log
data/forecasts.db   # the append-only log
```

Keep each module small (their coding rule: minimal lines that fully do the job). The dashboard reads the same SQLite file the pipeline writes.

---

## 2. Components: build + improve + observe

Each component below has: **Purpose · Build · Improvement loop · Pitfalls · Observe**.

### 2.1 Market connectors (ingestion)

**Purpose.** Uniform access to each venue: list open markets; get current price/order book; get each market's *resolution-criteria text*; poll for resolutions. Both selection (2.2) and settlement (2.7) depend on the criteria text, so capture it at ingest.

**Build.**
- Start with the venues that are legal and API-friendly. For paper-trading you can pull *live real odds* from Kalshi and Polymarket US (both accessible to you) and Manifold (open API, play-money). Real prices beat Manifold's biased play-money prices for validation — Manifold is only a convenience.
- Normalize every market into one record: `{venue, market_id, question, resolution_criteria, close_time, resolve_by, book}`.
- Snapshot the **full order book**, not just the top-of-book or mid — the fill simulator (2.5) needs the levels.
- Cache raw responses so you can replay pipeline logic without re-hitting APIs.

**Improvement loop.** Add venues only after one works end-to-end. Track per-venue data quality (missing criteria, stale books). Add cross-venue market matching later if you want to spot the same event priced differently (informational arbitrage), but that is a Phase-2+ luxury.

**Pitfalls.** Rate limits; silently truncated books; resolution-criteria text that's a link rather than inline (fetch it). Time zones on `resolve_by`.

**Observe.** Ingest count per run, per venue; % markets with usable criteria; book depth distribution; API error rate.

### 2.2 Selection / filter — the most important component

**Purpose.** Turn thousands of markets into the few dozen worth the expensive harness. **This is where the edge is created.** It is also your main cost lever: you only forecast survivors.

**Build.** A funnel with hard gates, in order:
1. **Vertical gate** — one domain you can build familiarity in (e.g., legislative/procedural, or corporate/product timing, or science/space timing). Everything else is dropped.
2. **Resolution-objectivity gate** — keep only markets that resolve on a specific, checkable rule or document. Drop vibes/opinion resolutions.
3. **Liquidity gate** — keep *low*-liquidity markets (few participants → weak aggregation → exploitable), but not so illiquid that the spread eats any edge. Record volume/open-interest and spread.
4. **Anti-efficiency gate** — explicitly exclude the efficient stuff regardless of vertical: macro prints (CPI/Fed/jobs), crypto price levels, major sports mainlines, election toplines. High volume, zero edge.
5. **Time-to-resolution preference** — bias toward days-to-weeks so you accumulate resolved samples fast.

Output: a ranked shortlist of candidate markets with their criteria and book.

**Improvement loop.** This is where the **selection ⇄ evaluation feedback** lives (see §3): once you have resolved forecasts, look at Δ_log *by category*. Tighten the filter toward the sub-categories where you beat the market; drop the ones where you don't. Selection is not static — it learns from where your edge actually shows up.

**Pitfalls.** Over-broad verticals scatter your sample so nothing reaches significance. Chasing volume (the opposite of the edge). Forgetting that document-resolved markets carry *resolution risk* — you can be right on the facts and lose on an ambiguous resolver, so read the criteria as carefully as the question.

**Observe.** Funnel counts (ingested → survived each gate → forecasted → traded); shortlist size per run; category mix; and later, Δ_log per category feeding back here.

### 2.3 Forecasting harness (the model)

**Purpose.** Produce a calibrated `p ∈ (0,1)` for each shortlisted market, **blind to `q`**, with a rationale and cited sources.

**Build.**
- **Inputs:** question + resolution criteria + researched context. **Not** the price.
- **Research step:** retrieve primary sources the crowd skims — the statute, docket, filing, launch manifest, FDA/clinicaltrials calendar, changelog, earnings transcript. This out-research is the mundane source of a better `p`.
- **Reasoning decomposition:** a structured pass — "reasons it resolves yes," "reasons no," "what is the default resolution if it resolved today," base rate, then a probability. (This mirrors the decompositions strong Metaculus bots use.)
- **Ensemble:** run several independent research/forecast passes and aggregate (trimmed mean). This is variance reduction — decorrelated estimators averaging toward truth — not magic.
- **Clamp** the output to `[0.01, 0.99]` (Appendix A explains why: log score is −∞ on a confident miss at 0/1).
- **Output contract:** `{p, rationale, sources}`.
- **Runtime:** build/run it in Claude Code on your subscription to subsidize cost; watch the shared 5-hour/weekly caps. Keep `ANTHROPIC_API_KEY` unset so it draws on the subscription, not metered API.

**Improvement loop.** Change **one thing at a time**, bump the version, reset the clock (§3):
- prompt / decomposition wording;
- number of ensemble members;
- research source mix (benchmark search providers against each other);
- post-hoc **recalibration** (learn a mapping from raw `p` to calibrated `p` from your own resolved history — see §3).

**Pitfalls.** Price leakage (the cardinal sin). Outputs at exactly 0/1. Letting the model "remember" outcomes for near-term questions it might have training exposure to — another reason forward-only matters. Research that's expensive but doesn't beat the baseline (check via 2.9).

**Observe.** `p` distribution (is it just clustering at 0.5, or at extremes?); average research cost per forecast; per-version calibration and log score; agreement/spread across ensemble members.

### 2.4 Decision & sizing layer

**Purpose.** Convert `(p, q)` into an action. This is where "only act where you disagree enough" lives. Distinct from both the forecaster and the evaluator.

**Build.** (See `decide()` in Appendix B.)
- Compute `e = p − q`.
- **Trade only if `|e| > spread + fee`.** Below that, the edge is eaten — pass.
- Size with **fractional Kelly**: for a "yes" buy, full-Kelly stake fraction is `e / (1 − q)`; use a fraction (0.25–0.5) of it. For "no", symmetric.
- Where `e ≈ 0`, **pass** — most of the time. Passing is the correct, most-common action.
- In sim, "action" = write an intended position + the simulated fill to the log.

**Improvement loop.** Tune the edge threshold and Kelly fraction using realized results: if you're well-calibrated but drawing down from variance, lower the Kelly fraction; if you're passing on everything, your harness may be echoing the price (go fix 2.3, don't loosen the threshold to force trades).

**Pitfalls.** Over-Kelly (variance ruin). Loosening the threshold to "get more action" — that just feeds fake edges into the record. Ignoring fees/spread.

**Observe.** Trade rate (fraction of shortlist actually acted on); edge distribution of taken vs passed; realized vs. theoretical stake sizes.

### 2.5 Paper execution / fill simulator

**Purpose.** Produce a realistic fill so paper results predict live results. Later, the seam where real execution would slot in.

**Build.** (See `sim_fill()` in Appendix B.)
- Walk the **snapshotted order book** for the side you're taking; return the volume-weighted **executable** price, never the mid.
- Apply fees.
- Maintain a paper portfolio: open positions, realized/unrealized paper P&L.

**Improvement loop.** Make fills more conservative over time (assume you get slightly worse prices than the snapshot). If you ever go to real money in a niche, reconcile real fills vs. simulated fills and recalibrate the simulator.

**Pitfalls.** Mid-price fills (systematically overstates edge). Assuming infinite size at top-of-book. Forgetting that in thin markets your own (hypothetical) size would move the price.

**Observe.** Simulated slippage (fill vs. mid) distribution; how often desired size exceeds available book.

### 2.6 Append-only forecast log — the scientific artifact

**Purpose.** The immutable record everything else reads from. The single easiest thing to under-build, and the thing the whole experiment's credibility rests on.

**Build.** One row per forecast (schema in Appendix B). Capture at forecast time: timestamp, market id, question, category/vertical, `p`, `q` (at that instant), `edge`, side, stake, **executable fill price**, spread, fee, **harness version**, rationale, sources, `resolve_by`, and the **baseline `p`**.
- **Immutability rule:** forecast-time fields never change. Settlement (2.7) is the *only* later write, and it may touch only `outcome`, `resolved_at`, `resolution_note`.
- Store in SQLite/DuckDB so the dashboard can query it directly.

**Improvement loop.** Add columns as you discover you want to slice by them (e.g., which search provider was used, ensemble spread) — but never rewrite history; new columns are NULL for old rows.

**Pitfalls.** Logging `q` as mid instead of the executable price you'd trade against (log both if unsure). Overwriting forecast-time fields when "fixing" something — that destroys the experiment. Not recording the version.

**Observe.** Row counts (total / open / resolved); write failures; schema-version drift.

### 2.7 Settlement tracker

**Purpose.** Come back days-to-months later, record the real `outcome` against each open forecast, and close the loop. Nothing gets scored until this runs.

**Build.**
- Poll each open market's resolution via the connector once past `resolve_by`.
- Write `outcome ∈ {0,1}`, `resolved_at`, and a `resolution_note`.
- **Flag ambiguous resolutions** for manual review (contested criteria, split resolutions) rather than silently recording them — resolution risk is real and you want it visible in the data.

**Improvement loop.** Improve ambiguity detection (e.g., flag when the market had a late price far from its final resolution, suggesting a contested close). Track your own "resolution surprises."

**Pitfalls.** Never running it (silent death of the whole pipeline). Auto-recording ambiguous resolutions as clean. Off-by-one on resolution dates.

**Observe.** **Settlement queue**: open positions past `resolve_by` (overdue flag); count of ambiguous/flagged resolutions; median time-to-resolution.

### 2.8 Evaluation & reconciliation engine

**Purpose.** Turn resolved rows into the numbers that answer "is there an edge, and where?" All definitions in Appendix A; code in Appendix B.

**Build.** Compute, over resolved forecasts:
- **Brier** and **log** score (yours, the market's, and the baseline's).
- **Market delta** `Δ_log` (and `Δ_Brier`) — *the headline metric*. Positive = you beat the price.
- **Bootstrap confidence interval** on `Δ_log` — so you never read a raw average as if it were certain.
- **Calibration curve** (reliability diagram) with binomial error bars.
- **Murphy decomposition** (reliability / resolution / uncertainty) — separates "honest probabilities" (reliability) from "discrimination" (resolution). The base-rate-parroter has high reliability but zero resolution; this is how you catch it.
- **Everything segmented by category and by harness version.**

**Improvement loop.** This engine *drives* every other improvement loop — it produces the signals §3 acts on. Add slices as questions arise (by time-to-resolution, by ensemble spread, by source mix).

**Pitfalls.** Reporting a global average that hides where the edge is (or isn't). Reading `Δ_log > 0` at N=15 as success. Mixing harness versions into one number.

**Observe.** This component *is* most of the dashboard (§4).

### 2.9 Baselines (cross-cutting)

**Purpose.** Cheap insurance against fooling yourself. If the fancy multi-agent harness doesn't beat these, you've learned that for almost nothing.

**Build.** Log alongside every real forecast:
- **Market baseline:** `baseline_p = q` (i.e., "do nothing / trust the price").
- **No-research baseline:** a single, cheap, one-shot LLM call with no retrieval.
Score both through the same evaluator.

**Improvement loop.** The **baseline ablation** (§3): the harness must beat *both* baselines on `Δ_log`, out of sample, by a margin that clears its own cost. If it only ties the market baseline, you have no tradeable edge.

**Pitfalls.** Skipping this and attributing noise to your clever pipeline.

**Observe.** Harness vs. each baseline on the same panels, side by side.

### 2.10 Orchestration + pre-registration

**Purpose.** Run the loop on a cadence, and enforce version discipline so results stay honest.

**Build.**
- A runner that: ingest → select → forecast → decide → sim → log, on a schedule (or manually at first — keep it simple).
- A **frozen `config.yaml`** = your pre-registration: the pinned harness version, vertical, thresholds, Kelly fraction. Committing a change bumps the version.
- Note: fully autonomous, unattended runs may fall under separate subscription terms than interactive Claude Code use — for now, running it semi-interactively (you kick it off, you iterate) is squarely covered and fine.

**Improvement loop.** The **outer scientific loop** itself (§3): the runner + config are the mechanism that makes "change one thing, bump version, reset clock" real rather than aspirational.

**Pitfalls.** Tweaking mid-sample and counting the whole record (p-hacking). Cron-driven autonomy before the logic is stable.

**Observe.** Run status/history; last successful run; current pinned version; cost/usage per run.

---

## 3. Improvement loops (the meta-process)

There are two levels: the **outer scientific loop** that governs the whole thing, and **per-component loops** it drives.

### The outer scientific loop

```
pre-register (freeze config, name a version)
      │
      v
run forward, log everything  ────────────┐
      │                                   │
      v                                   │ (weeks pass; positions resolve)
accumulate ~100+ resolved forecasts       │
      │  <───────────────────────────────┘
      v
score with the evaluator (Δ_log + CI, calibration, decomposition, by category+version)
      │
      v
diagnose: where is Δ>0? where is it <0? calibrated? discriminating? beating baselines?
      │
      v
change EXACTLY ONE thing  ──>  bump version  ──>  reset the clock  ──>  (back to run)
```

**The iron rule: every change to prompt/pipeline/selection/strategy creates a new version, and forecasts made under the old version do not count toward the new one.** Otherwise you will optimize on noise and manufacture a phantom edge. Keep versioned logs so you can always compare cleanly.

### Overfitting guardrails

- One change per version.
- Out-of-sample only: the CI on `Δ_log` is your honesty check — don't act on a result whose interval straddles zero.
- Prefer changes with a *mechanistic reason* (this source is primary, this decomposition reduces a known bias) over blind prompt-fiddling.
- Beware "improving" a metric on a small sample; wait for N.

### Per-component loop summary

| Component | Signal that drives change | Typical change |
|-----------|---------------------------|----------------|
| Selection (2.2) | `Δ_log` by category | tighten toward winning sub-categories; drop losers |
| Forecaster (2.3) | calibration + log score by version | prompt/decomposition, ensemble size, source mix, recalibration |
| Decision (2.4) | realized variance vs. calibration; trade rate | Kelly fraction; edge threshold |
| Fill sim (2.5) | real-vs-sim fill gap (if any live) | more conservative fills |
| Settlement (2.7) | resolution surprises | better ambiguity flags |

### Three named loops worth calling out

- **Selection ⇄ evaluation feedback.** Your edge is concentrated. Let category-level `Δ_log` reshape the selection filter: specialize where you win, kill where you lose. This is the highest-leverage loop.
- **Recalibration loop.** If your reliability diagram shows systematic over/under-confidence, learn a monotone mapping (isotonic/Platt) from raw `p` to calibrated `p` on your resolved history, and apply it going forward (as a new version). Cheap, often a real gain.
- **Baseline ablation loop.** Continuously ask: does the expensive harness beat (a) the price and (b) a no-research LLM call, out of sample, net of cost? If not, simplify — you've discovered the fancy part isn't paying.

### When to conclude "no edge"

If, after a few honest versions and 100+ resolved forecasts per version, `Δ_log` versus the market baseline has a confidence interval that includes zero (or is negative) across your categories — that is a *result*, not a failure. It means no tradeable edge in this niche with this harness, learned cheaply. Either switch niche or stop. This is the experiment working.

---

## 4. Observability — the web dashboard

### Philosophy

The dashboard exists to do four things, in priority order:
1. **Detect an edge with honest uncertainty** (foreground `Δ_log` + CI, not raw P&L).
2. **Localize it** (by category and version).
3. **Catch pipeline bugs and silent failures** (settlement queue, run health, cost).
4. **Prevent self-deception** (baselines side-by-side; N and CIs always visible).

Deliberately **de-emphasize cumulative paper P&L.** It is one noisy realized path and it invites exactly the wrong "am I up?" instinct. The average log-delta is the statistically efficient estimate; P&L is the tempting distraction. Show P&L small, show `Δ_log` big.

### Panels

1. **Headline — edge over the market.** `Δ_log` (cumulative and rolling) vs. market baseline, with a **bootstrap CI band**, plus a plain-language readout: *"N resolved · Δ_log = X · 95% CI [a, b] · not yet significant / significant."* This is the panel the whole project is about.
2. **Calibration.** Reliability diagram (predicted vs. observed) with **binomial error bars**; the 45° line; over/under-confidence called out. Alongside: Brier and log score for you vs. market vs. no-research baseline.
3. **Decomposition.** Murphy breakdown (reliability ↓ good, resolution ↑ good, uncertainty fixed) so you can see *why* your score is what it is — honesty vs. discrimination.
4. **By category & by version.** Small multiples of the headline metric split by category (where's the edge?) and by harness version (did the last change help?). Include a **version A/B on overlapping questions** where possible.
5. **Selection funnel.** Ingested → survived each gate → forecasted → traded, per run, with **cost per stage** (API/compute spend). Watch the harness only runs on survivors.
6. **Per-forecast explorer.** A searchable table: `ts, market, category, p, q, edge, side, stake, fill, status, outcome, version`, with drill-down to **rationale + sources**. Essential for debugging and for reading your reasoning on wins and losses.
7. **Open positions & settlement queue.** Open forecasts, `resolve_by`, and **overdue flags**; count of ambiguous/flagged resolutions.
8. **Ops / health.** Last run status, error log, throughput, and **API/subscription usage** (so you see when you're near caps).

### Alerts (push these; don't rely on remembering to look)

- Pipeline run failed / didn't run.
- Positions past `resolve_by` unsettled.
- Cost/usage spike or near subscription cap.
- Calibration drift beyond a band.
- `Δ_log` CI crosses zero (in either direction) — your significance status changed.

### Stack (pragmatic → scale-up)

- **Data layer:** query the **SQLite/DuckDB** log directly. No separate store needed at this scale.
- **Fastest to build:** **Streamlit** (Python, reads the DB, plots inline) — least code for a solo experiment. Or a **small React app** if you want a nicer web UI: fetch aggregates from a tiny FastAPI/SQLite backend and chart with **recharts** or **plotly**. Either is buildable in Claude Code as a single small app.
- **Compute the stats server-side** with the Appendix B functions; the front end just displays them.
- **Scale-up (optional, later):** Grafana over a SQL/time-series backend if you ever want richer alerting/history — overkill for now.
- Note on artifacts: if you prototype the dashboard as a browser artifact, don't use `localStorage`/`sessionStorage` — keep state in memory or read from your backend.

### Make observation possible at write-time

The dashboard can only show what you logged. The schema in Appendix B is designed so every panel above is a query away — capture `q`, executable fill, version, category, baseline, and sources **at forecast time**, or the panels can't be built retroactively.

---

## 5. Phased roadmap

**Phase 0 — plumbing + one vertical + baseline (paper).**
Connectors for one venue; the selection funnel for one vertical; the append-only log; the market baseline (`p = q`). No harness yet. Goal: markets flow in, get filtered, get logged, get settled. Prove the loop closes.

**Phase 1 — harness + scoring (paper, forward).**
Add the blind forecaster (with a no-research baseline), the decision layer, the fill sim, and the evaluator. Run forward. Goal: start accumulating resolved forecasts with real `Δ_log`.
*Gate:* pipeline stable, first ~30–50 resolutions in, scoring correct.

**Phase 2 — dashboard + improvement loops.**
Build the web dashboard (§4). Run the outer scientific loop: version discipline, category feedback into selection, recalibration, baseline ablation. Accumulate 100+ resolved per version.
*Gate:* a defensible `Δ_log` with a CI, per category, per version.

**Phase 3 — decision (only if edge is proven).**
If `Δ_log` beats both baselines out of sample with a CI clear of zero in some category — *and only then* — consider tiny real-money bets in that niche on a legal venue (Kalshi / Polymarket US), sized by fractional Kelly, capacity permitting. If not, switch niche or stop. Either outcome is a successful experiment.

---

## 6. Pitfalls / anti-patterns checklist

- [ ] **Price leakage** — forecaster sees `q` and echoes it. (Cardinal sin.)
- [ ] **Backtesting on resolved markets** — LLM contamination; forward-only.
- [ ] **Scoring at mid, no fees** — manufactures fake edge; use executable price.
- [ ] **Outputs at 0/1** — log score → −∞; clamp to [0.01, 0.99].
- [ ] **Counting pre-tweak forecasts** for a new version — p-hacking; reset the clock.
- [ ] **Chasing cumulative P&L** instead of `Δ_log` with a CI.
- [ ] **Reading small-N results as real** — wait for ~100+; show CIs.
- [ ] **Global averages** that hide where the edge is — segment by category+version.
- [ ] **Over-Kelly** — variance ruin; use a fraction.
- [ ] **Loosening the edge threshold to "get action"** — feeds noise into the record.
- [ ] **Ignoring resolution risk** — right on facts, lost on an ambiguous resolver; read criteria carefully; flag ambiguity.
- [ ] **Chasing volume** (efficient markets) — the opposite of the edge.
- [ ] **Never running settlement** — silent death of the experiment.
- [ ] **Mutating forecast-time log fields** — destroys the scientific record.
- [ ] **Skipping baselines** — can't tell if the fancy part earns its keep.

---

## Appendix A — the math

Two ingredients per forecast: your probability `p` and the outcome `o ∈ {0,1}`. The market's probability at the same instant is `q`.

**Brier score** (MSE on probabilities; 0–1, lower better; 0.25 = always saying 0.5):
```
BS = mean( (p − o)^2 )
```
Proper: minimized in expectation only by reporting true belief.

**Log score** (log of the probability placed on what happened; ≤0, higher/closer-to-0 better):
```
LS = mean( o·ln(p) + (1−o)·ln(1−p) )
```
Unbounded below: a confident miss at `p=0/1` gives −∞. Hence **clamp to [0.01, 0.99]**. Negated, this is log loss / cross-entropy.

Brier is bounded and forgiving of overconfidence; log is brutal at the tails and rewards genuine confidence when right. Report both.

**Calibration / reliability diagram.** Bin forecasts by `p`; plot mean predicted vs. observed frequency per bin. Perfect = 45° line. Put binomial error bars on each bin (≈10 points/bin at N=100 is noisy). Calibration is **necessary but not sufficient**: a base-rate parroter is perfectly calibrated and useless — which is why you also need discrimination, made precise by:

**Murphy decomposition of Brier:**
```
BS = Reliability − Resolution + Uncertainty
Reliability = mean_k n_k (p̄_k − ō_k)^2      # want LOW  (calibration-curve gap, squared)
Resolution  = mean_k n_k (ō_k − ō)^2         # want HIGH (how far bins separate from base rate)
Uncertainty = ō(1 − ō)                       # fixed by the world
```
`ō` = overall base rate; `ō_k`, `p̄_k` = observed freq and mean forecast in bin k; `n_k` = bin count. Reliability is your calibration curve as a number; Resolution is the discrimination the parroter lacks.

**Edge and expected value.** Buying "yes" at price `q` (pay `q`, collect 1 if it happens), under your belief `p`:
```
EV per contract = p·(1 − q) − (1 − p)·q = p − q  =  e
```
So `e = p − q` is your disagreement, your expected profit per unit, and (below) your growth rate — all one number. `e ≈ 0` ⇒ no bet.

**Fractional Kelly sizing** (binary, buying "yes" at `q`, belief `p`):
```
full-Kelly stake fraction  f* = (p − q) / (1 − q)          # "no" side: (q − p)/q
use   f = kelly_fraction · f*     with kelly_fraction ∈ [0.25, 0.5]
```

**Market delta — the headline.** Score the market with the identical rule on the identical events and subtract:
```
Δ_Brier = mean( (q − o)^2 − (p − o)^2 )     # >0 ⇒ you beat market (closer to outcome)
Δ_log   = mean( [o·ln p + (1−o)·ln(1−p)] − [o·ln q + (1−o)·ln(1−q)] )   # >0 ⇒ you beat market
```
If `p = q`, both are 0 — matching the market buys nothing.

**Why `Δ_log` is the metric for a trading bot.** Take the per-event log advantage `o·ln(p/q) + (1−o)·ln((1−p)/(1−q))` and its expectation under the true probability `r`:
```
E[Δ_log] = KL(r ‖ q) − KL(r ‖ p)
```
i.e. *how far the market sits from truth* minus *how far you sit from truth*. If you're perfect (`p=r`) this is `KL(r‖q) ≥ 0` — the market's own error, the ceiling on what you can harvest. If `p=q` it's 0. Crucially, a full-Kelly bettor's expected **log-bankroll growth per bet is exactly this same quantity**. So **your average log-score margin over the market is the exponential growth rate of a Kelly bankroll.** That's why the dashboard foregrounds `Δ_log`: paper P&L is one noisy draw from this rate; the average `Δ_log` estimates the rate itself. You never observe `r`, so the empirical average is an *estimate* with error ~1/√N — hence the 100+ sample gate and the bootstrap CI.

---

## Appendix B — schema + core code

### SQLite schema (the append-only log)

```sql
CREATE TABLE forecasts (
  id              INTEGER PRIMARY KEY,
  ts_forecast     TEXT NOT NULL,      -- ISO8601, when p was made
  venue           TEXT,
  market_id       TEXT,
  question        TEXT,
  category        TEXT,               -- for segmentation
  vertical        TEXT,
  p               REAL NOT NULL,      -- your probability (clamped)
  q               REAL NOT NULL,      -- market prob at ts_forecast
  edge            REAL,               -- p - q
  side            TEXT,               -- 'yes' | 'no' | 'pass'
  stake           REAL,               -- sim units / bankroll fraction
  fill_price      REAL,               -- EXECUTABLE price (not mid)
  spread          REAL,
  fee             REAL,
  harness_version TEXT NOT NULL,      -- resets the clock on change
  rationale       TEXT,
  sources         TEXT,               -- JSON list
  baseline_p      REAL,               -- dumb baseline (e.g. = q, or no-research call)
  resolve_by      TEXT,               -- expected resolution date
  resolved_at     TEXT,               -- set at settlement ONLY
  outcome         INTEGER,            -- 1|0, NULL until resolved  (settlement ONLY)
  resolution_note TEXT                -- ambiguity flag / note      (settlement ONLY)
);
-- Immutability: after insert, only settlement may write resolved_at, outcome, resolution_note.
```

### Core scoring (minimal, complete)

```python
import numpy as np
EPS = 0.01
clamp = lambda p: min(1 - EPS, max(EPS, p))

def brier(p, o):                       # arrays; lower better
    return np.mean((np.asarray(p) - np.asarray(o)) ** 2)

def log_score(p, o):                   # higher (->0) better
    p = np.clip(p, EPS, 1 - EPS); o = np.asarray(o)
    return np.mean(o * np.log(p) + (1 - o) * np.log(1 - p))

def delta_log(p, q, o):                # >0 => you beat the market
    return log_score(p, o) - log_score(q, o)

def calibration(p, o, bins=10):        # -> [(mean_pred, obs_freq, n), ...]
    p, o = np.asarray(p), np.asarray(o)
    idx = np.clip(np.digitize(p, np.linspace(0, 1, bins + 1)) - 1, 0, bins - 1)
    return [(p[idx == b].mean(), o[idx == b].mean(), int((idx == b).sum()))
            for b in range(bins) if (idx == b).any()]

def murphy(p, o, bins=10):             # BS = rel - res + unc
    p, o = np.asarray(p), np.asarray(o); N = len(o); ob = o.mean()
    idx = np.clip(np.digitize(p, np.linspace(0, 1, bins + 1)) - 1, 0, bins - 1)
    rel = res = 0.0
    for b in range(bins):
        m = idx == b; n = m.sum()
        if n:
            rel += n * (p[m].mean() - o[m].mean()) ** 2
            res += n * (o[m].mean() - ob) ** 2
    return dict(reliability=rel / N, resolution=res / N, uncertainty=ob * (1 - ob))

def bootstrap_ci(p, q, o, n=2000, a=0.05):     # CI on delta_log
    p, q, o = map(np.asarray, (p, q, o)); k = len(o)
    s = [delta_log(p[i], q[i], o[i]) for i in
         (np.random.randint(0, k, k) for _ in range(n))]
    return tuple(np.quantile(s, [a / 2, 1 - a / 2]))
```

### Forecaster interface (blind to q)

```python
def forecast(question, resolution_criteria, context) -> dict:
    """Return {'p': float in (0,1), 'rationale': str, 'sources': list}.
    MUST NOT receive q or any market price. Ensemble + clamp happen inside."""
    ...
```

### Decision + fill sim

```python
def decide(p, q, spread, fee, kelly=0.25, bankroll=1.0):
    e = p - q
    if abs(e) <= spread + fee:            # edge eaten -> pass
        return ('pass', 0.0)
    if e > 0:  return ('yes', kelly * (e / (1 - q)) * bankroll)
    else:      return ('no',  kelly * ((-e) / q)   * bankroll)

def sim_fill(book, side, size):
    """Volume-weighted EXECUTABLE price walking the real book snapshot. Never mid."""
    levels = book['asks'] if side == 'yes' else book['bids']
    filled, cost = 0.0, 0.0
    for price, qty in levels:
        take = min(qty, size - filled); cost += take * price; filled += take
        if filled >= size: break
    return cost / filled if filled else None
```

---

## Appendix C — resources

**Research anchor.** Halawi, Zhang, Yueh-Han & Steinhardt, *Approaching Human-Level Forecasting with Language Models* (NeurIPS 2024, arXiv 2402.18563). Honest headline: the system *nears* the human crowd aggregate, surpassing only in some settings. Uses a strictly-post-cutoff test set to prevent leakage — the same forward-only discipline this doc mandates.

**Build / community.** Metaculus `forecasting-tools` repo and the "build a bot in 30 minutes" tutorial (EA Forum / LessWrong); the AI Forecasting Benchmark / FutureEval tournament as a free live test venue. `faintsignals.substack.com` "Building an AI Prediction Bot" as an honest worked example. Ozzie Gooen / QURI for methodology.

**Critical writing.** Nuño Sempere's *Forecasting Newsletter* (forecasting.substack.com) and his essays on incentive/alignment problems in forecasting platforms; his org **Sentinel** as a live LLM-assisted + human-forecaster operation.

**Trader side.** Domer interviews (research process, small bets, independent opinions, informational arbitrage). Podcast: *Prediction Market Movers* — esp. the FutureSearch (Dan Schwarz) episode on writing high-quality questions, and the Oddpool episode on prediction-market data/quant/backtesting. *Odd Lots* for reputable analysis; Bloomberg's Big Take on prediction-market "insider" traders as a reality check on why some wins look superhuman.

**Skip:** AI-generated daily "prediction bets" tip podcasts; venue-run entertainment shows; most "N strategies that actually work" SEO listicles; copy-trading/whale-following (market-maker wallets just capture the spread you'd pay; huge one-off wins are insider info you can't replicate).

---

*Build order, condensed: close the loop on paper first (ingest → select → log → settle), add the blind harness and the evaluator, then the dashboard, then iterate one version at a time until `Δ_log` has an honest confidence interval. The number that decides everything is your log-score margin over the market — everything in this document exists to estimate it without fooling yourself.*
