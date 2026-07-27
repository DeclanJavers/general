# Prediction-Market Forecasting Bot — Design & Build Doc (v2.1, evidence-revised, Claude Agent SDK build)

> **Thesis in one line:** you are not building a model that predicts outcomes. You are building a *disagreement detector with a track record* — something that identifies where the market price is wrong, in corners where sharp money isn't looking, and proves out-of-sample that it's right when it disagrees.
>
> **v2 amendment:** the evidence says the surviving version of this thesis is narrower: a **rules-literate, maker-side, breadth-over-depth** operation on fast-resolving document-resolved markets — because per-market capacity is tiny, taker execution is where retail money dies, and the single most consistently attested repeatable edge is reading resolution criteria that other traders don't.

This document is the full plan: every component, how to build it, how to improve it, how to watch it in a web dashboard, plus a self-contained math appendix, minimal code scaffolds, and the evidence base (Appendix D) behind every major design decision. v2 incorporates a six-track research review (venues/APIs, LLM forecasting evidence, market-efficiency evidence, execution microstructure, evaluation statistics, project postmortems) conducted 2026-07-27. v2.1 specifies the **Claude Agent SDK** (`claude-agent-sdk`) as the harness runtime — see the implementation map in §1 — which turns two of the plan's guardrails (price-blindness, output-format validation) from discipline into mechanism.

---

## Table of contents

0. [Read this first — the core thesis](#0-read-this-first--the-core-thesis)
0.5 [What the evidence changed (v1 → v2)](#05-what-the-evidence-changed-v1--v2)
1. [System overview](#1-system-overview)
2. [Components: build + improve + observe](#2-components-build--improve--observe)
3. [Improvement loops (the meta-process)](#3-improvement-loops-the-meta-process)
4. [Observability — the web dashboard](#4-observability--the-web-dashboard)
5. [Phased roadmap](#5-phased-roadmap)
6. [Pitfalls / anti-patterns checklist](#6-pitfalls--anti-patterns-checklist)
- [Appendix A — the math](#appendix-a--the-math)
- [Appendix B — schema + core code](#appendix-b--schema--core-code)
- [Appendix C — resources](#appendix-c--resources)
- [Appendix D — the evidence base](#appendix-d--the-evidence-base)

---

## 0. Read this first — the core thesis

Every design decision below follows from these. Re-read them whenever a choice feels arbitrary.

1. **The objective is the market's error, not the outcome.** The scalar that matters is the **edge** `e = p − q`, where `p` is your probability and `q` is the market's. A model that reproduces `q` has predicted "zero error everywhere" and is worthless — correctly, it will never bet. Usefulness lives entirely in the gap.

2. **The edge is selection + rules-literacy + execution posture, not foresight.** You win by being disciplined about *where* you compete (thin, fast-resolving, document-resolved markets with unambiguous criteria) and *how* you execute (maker, favorite side) far more than by being smart when you compete. Most questions you should skip. **Evidence note:** no published system — including Bridgewater's AIA Forecaster, which matches superforecasters — beats liquid real-money market prices head-on (D.2, D.6). The documented repeatable edges are (a) reading resolution fine print the crowd skims, (b) maker-vs-taker execution (~22 ROI points on Kalshi), and (c) breadth across small mispricings that are individually capacity-limited to tens or hundreds of dollars (D.3, D.4).

3. **The forecaster must be blind to the price — and blindness must be enforced in the research layer.** If the harness sees `q`, it will anchor and echo it (LLM anchoring to human/market medians is documented, D.2). Not passing `q` is insufficient: web research surfaces prices via venue pages and news articles quoting odds. The research tool needs a **domain blocklist** and a post-hoc contamination scan, and contaminated forecasts get flagged in the log.

4. **The real goal of this project is to find out cheaply whether an edge exists.** Success = a defensible answer to "does my harness beat the market's log score out-of-sample in one niche?" — not a bankroll. **Evidence note:** the projects that died, died of *sample-size purgatory* (no iteration signal) and ops fragility, not API costs — real cost is ~$0.50–$2/question (D.6). Build the evaluation harness before the bot.

5. **The objective function is trading EV, not tournament rank.** These diverge (tournament scoring rewards extremizing and question cherry-picking; trading rewards calibrated disagreement net of fees — D.6). Tournaments like Metaculus FutureEval are a useful *free scoring signal*, but every design choice here optimizes Δ_log against executable prices.

6. **Constraints while paper-trading:**
   - **Forward only.** Never backtest on already-resolved markets: the LLM either saw the outcome in training or can look it up. Every published "superhuman forecasting" claim rested on contaminated backtests and none replicated forward (D.2). The only clean signal is live forecasts on questions unresolved at forecast time.
   - **Score the executable price, not the mid.** Simulate fills against the real order book, with trade-through logic for maker orders, fees per venue/series, and an adverse-selection haircut. Expect live to run 20–50% below even a good sim (D.4).
   - **Sample size gate — counted in clusters, not markets.** The v1 "100+" figure was optimistic. Realistic per-event variance means an edge of 0.05 nats needs ~300–500 *effective* (cluster-level) resolutions for 80% power; 0.01 nats is a multi-thousand-event project (Appendix A, D.5). Markets resolving on the same underlying event count once. Pre-register your minimum edge of interest and the corresponding N.

**Design principles that fall out of the above:**

- *Independence:* forecaster never sees `q`; research layer enforces it.
- *Append-only truth:* the forecast log is the scientific artifact; forecast-time fields are immutable.
- *Versioning without purgatory:* every change to the forecaster is a new harness version — but versions are compared by **running them concurrently on the same questions** (paired A/B), and decision-layer/recalibration changes are validated by **replay on logged history**, so most improvements don't burn calendar time (§3).
- *Baselines everywhere:* always log the market baseline (`p = q`) and a single no-research frontier-model call. The no-research baseline is a serious contender, not a strawman — base-model quality dominates scaffolding (D.2).
- *Segment by category and version, report with honest uncertainty:* cluster-aware CIs, always-valid confidence sequences for continuous monitoring, empirical-Bayes shrinkage before believing any per-category number (§3, Appendix A).

---

## 0.5 What the evidence changed (v1 → v2)

The six-track research review (full findings in Appendix D) forced these revisions:

1. **"Thin = exploitable" is not automatic.** Tetlock (2008): liquidity does not reduce — and sometimes increases — mispricing. Thin markets offer worse *prices* but tiny *capacity* (median exploitable arb: ~15 shares). The thesis survives only as **breadth**: many small, fast-resolving, uncorrelated positions. → Selection funnel now has capacity-realism and throughput gates (§2.2).
2. **Maker vs taker is most of the game.** Kalshi data (314k contracts): makers −9.6% avg return vs takers −31.5%; makers on ≥50¢ contracts were the *only* positive bucket (+2.6%). Sub-10¢ longshot buyers lose >60%. → Decision layer redesigned maker-first with a side-of-bias gate (§2.4); fill sim redesigned around trade-through fills (§2.5).
3. **The power math was optimistic.** Var(per-event Δ_log) ≈ 2·Δ_log *at best*, inflating 2.5–8× for noisy forecasters; N ≈ 15.7/Δ_log is the floor. → Pre-registered power analysis, cluster-level counting, paired concurrent A/B, confidence sequences (§3, Appendix A).
4. **The bar is higher than assumed.** Nobody credibly beats liquid real-money prices; FutureSearch's professional $100K Kalshi operation (edge threshold + honest book-walking — essentially this design) found liquidity the binding constraint and its Polymarket paper book is *losing*. → Expectations and Phase-3 gates tightened; the experiment framing ("is there an edge in this niche?") is the product.
5. **The most attested edge is rules-literacy.** "Traders read the title, not the resolution criteria" recurs across practitioner accounts; LLM bots' top failure mode is *also* misreading criteria. → New first-class harness step: resolution-criteria interpretation pass (§2.3); vertical ranking now leads with fine-print-heavy categories (§2.2).
6. **Resolution risk is a fat tail, not a nuisance.** UMA token-holder votes have reversed correct positions on $7M and $237M Polymarket markets; ~1,150 disputed markets in 2026 YTD; Kalshi's centralized resolution has wording-ambiguity failures but no oracle attack surface. → Kalshi is the primary venue; dispute-risk screening is the hardest selection gate (§2.2, §2.7).
7. **Theta is priced.** Prices embed a ~3–7% annualized settlement discount; 48–88% of apparent long-horizon "miscalibration" is just that. Kalshi pays ~4% APY on cash *and open positions*, largely neutralizing it there. → Theta gate in selection; prefer <60–90 days to resolution (§2.2).
8. **The harness recipe is now evidence-ranked, not folklore:** frontier base model > agentic multi-source retrieval > cross-family ensemble of 6–8 (trimmed mean) > explicit base rates > output capping ~[0.03, 0.97] > Platt recalibration later. LLMs are *under*-extreme (hug 50%), not overconfident. Fine-tuning: skip. (§2.3, D.2)
9. **Ops kill projects, not models.** Silent retrieval failures → hallucinated forecasts; format/units bugs cost tournament winners whole seasons; "make sure it runs all the time" is the most-cited advice. → Retrieval health assertions, format validation, and run-health observability are first-class (§2.3, §4).
10. **Don't build the plumbing from scratch.** Metaculus `forecasting-tools`/`metac-bot-template` is maintained (July 2026), battle-tested, and free; Polymarket's official `agents` repo is an archived graveyard. → v2.1: the agentic research loop comes from the Claude Agent SDK (§1 map); `forecasting-tools` covers Metaculus plumbing and non-Claude ensemble members; the archived repo stays ignored (§2.3, D.6).
11. **Venue facts (live-verified):** Kalshi REST market data needs no auth, books are full-depth, rules text is inline — but you must pass `mve_filter=exclude` or 99.3% of the "universe" is auto-generated parlay legs; 78.5% of quoted markets had zero 24h volume (the thin tail is *very* thin). Polymarket international is read-only for US persons (ToS); Gamma pagination is quirky (keyset only past 5k offset) and its prices are stale vs the CLOB. Fees on both venues are now per-category/per-series and changed twice in six months — pull them from the API, never hard-code. Manifold is the only venue where the full loop (order → resolution) can legally run today, so it's the integration testbed, nothing more. (§2.1, D.1)

---

## 1. System overview

### Pipeline

```
                 ┌─────────────────── improvement loop ───────────────────┐
                 v                                                         │
  ingest ──> select ──> forecast(BLIND to q) ──> decide(edge+size) ──> sim-execute
   (2.1)     (2.2)          (2.3)                    (2.4)               (2.5)
                                                     maker-first        trade-through
                                                                          │
                                                                          v
                                                                        LOG (2.6)  <── append-only
                                                                          │
                                                                     [ wait days/weeks ]
                                                                          │
                                                                          v
                                                                     settle (2.7)
                                                                          │
                                                                          v
                                                                    evaluate (2.8) ──> dashboard (§4)
                                                                          │
                                                                          └──> diagnose ──> change (paired A/B) ──┘
```

`q` is computed alongside the forecaster (from the same market data) and injected only at `decide`. The forecaster receives the question, resolution criteria, and researched context — never the price, with the research layer's domain blocklist enforcing it.

### Component map

| # | Component | Purpose | Criticality |
|---|-----------|---------|-------------|
| 2.1 | Market connectors | Pull markets, prices, order books, resolution criteria, fee params, outcomes | High (foundation) |
| 2.2 | Selection / filter | Funnel universe down to exploitable questions; control cost | **Highest** (this *is* the edge) |
| 2.3 | Forecasting harness | Produce `p` + rationale + sources, blind to `q` | High |
| 2.4 | Decision & sizing | Maker-first; act only past evidence-based thresholds; fractional Kelly on shrunk `p` | High |
| 2.5 | Paper execution / fill sim | Trade-through fills, adverse-selection haircuts, markout logging | High (upgraded from Medium — sim realism is where fake edges are born) |
| 2.6 | Append-only log | The scientific record everything reads from | **Highest** (easy to under-build) |
| 2.7 | Settlement tracker | Close the loop; record outcomes; flag ambiguity/disputes | High |
| 2.8 | Evaluation engine | Δ_log with cluster-aware CIs, confidence sequences, calibration, decomposition, by category+version | **Highest** (build before the bot — D.6) |
| 2.9 | Baselines | Prove the harness beats "trust the price" and "no-research frontier call" | Medium (cheap insurance) |
| 2.10 | Orchestration + pre-registration | Run on a cadence; freeze the spec; version discipline | Medium |

### Suggested repo layout (minimal)

```
bot/
  connectors.py     # 2.1  fetch markets, books, fee params, resolutions
  select.py         # 2.2  the funnel
  forecast.py       # 2.3  the harness (blind; Claude Agent SDK agents + non-Claude members)
  decide.py         # 2.4  edge thresholds + maker/taker posture + sizing
  execute.py        # 2.5  fill simulation (trade-through), paper portfolio, markouts
  log.py            # 2.6  append-only writes over SQLite
  settle.py         # 2.7  poll resolutions; dispute flags
  evaluate.py       # 2.8  scoring, cluster bootstrap, confidence sequences (Appendix B)
  baselines.py      # 2.9
  run.py            # 2.10 orchestration; reads a frozen config.yaml (the pre-registration)
  config.yaml       # pinned harness version, vertical, thresholds, kelly fraction, min-edge + target N
dashboard/          # §4  web app reading the SQLite log
data/forecasts.db   # the append-only log
data/raw/           # cached raw API responses (replayable)
```

Keep each module small. The dashboard reads the same SQLite file the pipeline writes.

### Claude Agent SDK implementation map

The harness and its guardrails are specified against the **Claude Agent SDK** (`pip install claude-agent-sdk`; docs at code.claude.com/docs/en/agent-sdk). The pipeline itself stays plain Python — the SDK appears only inside `forecast.py`. What each design requirement maps to:

| Design requirement | SDK mechanism |
|---|---|
| Blind, isolated ensemble members | independent `query()` calls — each is a fresh session with no shared context; parallelize with `asyncio.gather` |
| Price-blindness enforcement (§2.3) | `PreToolUse` hook on `WebSearch`/`WebFetch` returning `permissionDecision: "deny"` for blocklisted domains — mechanical, not aspirational |
| Contamination scan (§2.3) | `PostToolUse` hook scanning fetched content for price-like mentions of the market → sets the `contaminated` log flag |
| Agentic multi-step research | built-in `WebSearch` + `WebFetch` tools; `allowed_tools` restricted to exactly these plus the custom MCP tools |
| Safe internal data access | in-process MCP server (`create_sdk_mcp_server` + `@tool`) exposing the cached resolution-criteria / primary-doc fetchers — never price fields |
| Validated output contract (§2.3) | `output_format={"type": "json_schema", ...}` with a pydantic schema; `error_max_structured_output_retries` handled as a dropped member — kills the format-bug failure mode (D.2) mechanically |
| Headless runs, no permission prompts | `permission_mode="dontAsk"` + explicit `allowed_tools` |
| Runaway protection | `max_turns` + `max_budget_usd` per member |
| Cost logging (§4 panels) | `ResultMessage.total_cost_usd` + `usage` per member, written to the log |
| Cross-family ensemble (D.2) | the SDK drives Claude models only (`model="opus"`, `"sonnet"`, …); non-Claude members run as plain API calls (e.g., via litellm / `forecasting-tools`' GeneralLlm) behind the same `forecast()` contract |

Two consequences worth naming. First, `forecasting-tools` keeps a narrower role than the v2 draft gave it: Metaculus tournament plumbing (if entering FutureEval) and the wrapper for non-Claude ensemble members — the agentic research loop itself comes from the Agent SDK. Second, **billing**: the SDK requires `ANTHROPIC_API_KEY`; Claude-subscription (Claude Code login) auth is not supported for SDK automation, so automated harness runs are metered API usage. At the evidence-based operating point (~$1–1.50/question, D.6) this is low-hundreds-per-quarter money, capped per-forecast by `max_budget_usd` and per-run by the orchestrator.

---

## 2. Components: build + improve + observe

Each component below has: **Purpose · Build · Improvement loop · Pitfalls · Observe**. Evidence references point into Appendix D.

### 2.1 Market connectors (ingestion)

**Purpose.** Uniform access to each venue: list open markets; get current price/order book; get each market's *resolution-criteria text*; get *fee parameters*; poll for resolutions. Both selection (2.2) and settlement (2.7) depend on the criteria text, so capture it at ingest.

**Build.**
- **Kalshi is the primary venue** (D.1): US-legal end-to-end, read-only REST needs no API key, order books are full depth, rules text is inline (`rules_primary`/`rules_secondary`), candlesticks give free price history, there's an official demo environment for plumbing tests, and it pays ~4% APY on cash and open positions (kills most of the theta problem). Bot trading is officially supported.
  - **Always pass `mve_filter=exclude`** on market listing — without it, 99.3% of returned "markets" are auto-generated parlay legs with empty books (live-verified). Expect ~65–70k real open markets.
  - The API is mid-migration from integer cents to fixed-point dollar strings; deprecated fields (e.g., `liquidity`) now return 0 — code against the `*_dollars`/`*_fp` fields; most 2024-era sample code is broken.
  - Rate limits: ~20 reads/s sustained on the basic tier — ample. WebSocket requires API-key auth even for market data; REST doesn't.
  - **Pull fee parameters per series from the API** (`GET /series` fee fields / series fee-changes endpoint). Fees are series-specific and have changed twice in six months. Never hard-code.
- **Polymarket international is a read-only signal layer.** US persons are ToS-prohibited from trading (orders from US IPs are close-only) but market data is not geo-blocked. Use Gamma for metadata (criteria text is inline in `description`), the CLOB API for live books (`/book` is full depth; Gamma's price fields are stale), and the public WebSocket for updates. Gamma pagination: `limit` is silently capped at 100 and `offset` at ~5,000 — full scans must use keyset pagination. Numeric fields arrive as JSON-encoded strings; parse defensively. Polymarket US (the CFTC-regulated venue, taker θ=0.06, maker rebate) is the later real-money path there if wanted.
- **Manifold is the integration testbed only**: the one venue where the full loop (order → resolution → P&L) legally runs today, bots explicitly welcome, no auth for reads. But it's an AMM (no order book/depth concept), creator-resolved, play-money. Don't calibrate against it; its inefficiencies (default-50% pricing on fresh markets, benign counterparties) are exactly what real venues lack (D.6).
- Normalize every market into one record: `{venue, market_id, question, resolution_criteria, close_time, resolve_by, book, fee_params, category}`.
- Snapshot the **full order book at decision time** (books in the thin tail go stale/one-sided; candlesticks don't exist where no trades happened).
- **Cache raw responses append-only** so pipeline logic is replayable without re-hitting APIs — this is what makes decision-layer changes testable without new forecasts (§3).

**Improvement loop.** Add venues only after one works end-to-end. Track per-venue data quality (missing criteria, stale books). Cross-venue matching (same event priced differently on Kalshi vs Polymarket) is a cheap forecast *feature* and calibration check — Phase-2 luxury.

**Pitfalls.** The MVE-parlay flood; deprecated-field zeros; Gamma pagination caps; treating Gamma prices as live; time zones on `resolve_by`; fee schedules drifting under you.

**Observe.** Ingest count per run, per venue; % markets with usable criteria; book depth distribution; API error rate; fee-schedule change events.

### 2.2 Selection / filter — the most important component

**Purpose.** Turn tens of thousands of markets into the few dozen worth the expensive harness. **This is where the edge is created.** It is also your main cost lever: you only forecast survivors.

**Build.** A funnel with hard gates, in order:

1. **Vertical gate** — one domain, chosen by evidence-ranked promise (D.3):
   - *Tier 1:* **document/rules-resolved markets where the title diverges from the resolution text** — procedural/legislative, regulatory/legal filings, corporate/product timing. "Traders read the title, not the criteria" is the most consistently attested repeatable inefficiency, and parsing fine print + primary documents at scale is exactly what an LLM pipeline is good at.
   - *Tier 2:* awards/pop-culture and one-off novelty markets (worst measured calibration, low sharp attention, usually objectively resolved).
   - *Tier 3:* science/space/tech timing (schedule-slip base rates beat vibes).
   - *Avoid:* Fed/rates and CPI (near-perfect calibration), election toplines, sports mainlines, crypto prices, and anything arb-shaped (latency games owned by specialists).
2. **Resolution-clarity gate — the hardest gate** (D.3). Keep only markets with unambiguous wording, a named authoritative source, and no plausible dispute angle. On Polymarket-observed questions, UMA dispute risk is a fat tail that has fully reversed correct positions; the top practitioner's rule is to avoid disputable markets "like the plague." Kalshi's centralized resolution is safer for document-resolved theses but read its rulebook wording as carefully as the question — its failures are wording-ambiguity failures.
3. **Side-of-bias gate** (D.3, D.4). The structural tailwind is *selling longshots / being on the ≥50¢ favorite side* — the only bucket with documented positive post-fee returns. Buying sub-10¢ longshots as a taker requires the market to be wrong by >2.5× just to break even; demand overwhelming evidence.
4. **Theta gate** (D.3). Required edge must clear the annualized settlement-discount hurdle (~5–8%/yr on Polymarket-style venues; much less on Kalshi thanks to ~4% APY on open positions) plus fees. Strongly prefer **<60–90 days to resolution** — it also feeds the sample-size engine faster.
5. **Liquidity/capacity gate.** Keep low-liquidity markets (weak price discovery — the sharp 3% who create calibration aren't there, D.3) but not no-liquidity ones: require a two-sided book and spread below a cap (a practitioner-validated tradability filter is max ~5¢ spread), and record that realistic fillable size is $100–$1,000, not $10k (D.4). Remember 78.5% of quoted Kalshi markets had zero 24h volume — most of the tail fails this gate.
6. **Throughput requirement (new, structural).** The chosen vertical must yield enough qualifying, fast-resolving markets to hit the pre-registered sample size in tolerable calendar time — target ≥ 25–30 *cluster-level* resolutions/month. Verify against the venue's actual market flow before committing; a vertical that yields 5/month makes the experiment undecidable (Appendix A).

Output: a ranked shortlist of candidate markets with their criteria, book, fee params, and **cluster ID** (the underlying real-world event — assigned here, at selection time, because it cannot be reconstructed later and the evaluator needs it).

**Improvement loop.** This is where the **selection ⇄ evaluation feedback** lives (§3): once resolutions accumulate, look at Δ_log by category — but only through **empirical-Bayes shrinkage** (Appendix A), and confirm any promote/drop decision on a time-split before acting. Raw per-category winners at small N are winner's-curse artifacts; expect the measured edge in promoted categories to shrink — that's regression to the mean, not decay.

**Pitfalls.** Over-broad verticals scatter the sample. Chasing volume. Believing thin = exploitable without checking capacity. Treating a long-dated near-certainty discount as mispricing (it's priced theta). Skipping cluster assignment.

**Observe.** Funnel counts (ingested → survived each gate → forecasted → traded); shortlist size per run; category mix; cluster-per-month rate vs the throughput target; and later, shrunken Δ_log per category feeding back here.

### 2.3 Forecasting harness (the model)

**Purpose.** Produce a calibrated `p ∈ (0,1)` for each shortlisted market, **blind to `q`**, with a rationale and cited sources.

**Build.** The recipe below is evidence-ranked from tournament results and ablations (D.2) — in descending order of measured impact:

- **Frontier base model first.** Model choice dominates scaffolding: a plain template bot on the best reasoning model beat ~94 custom bots in Q2 2025; prompting cannot rescue weaker models. Re-benchmark model choice quarterly. Scaffolding on top of a frontier model is still worth ~5–11 peer-score points (≈9 months of model progress).
- **Build each member on the Claude Agent SDK** (implementation map in §1): one independent `query()` per ensemble member — fresh session, no shared context — with `allowed_tools` restricted to `WebSearch`, `WebFetch`, and the internal MCP tools; `permission_mode="dontAsk"` for headless runs; a JSON-schema `output_format` enforcing the output contract; `max_turns`/`max_budget_usd` as runaway guards. Structured-output validation is first-class in the SDK, which mechanically retires the format/units failure mode that cost tournament builders whole seasons (D.2). Keep `forecasting-tools` for Metaculus tournament plumbing and as the wrapper for non-Claude ensemble members — don't rebuild either.
- **Agentic multi-source retrieval** — the largest single component win (ablations: ~0.027 Brier; removing search degrades ~3.6×). Iterative search beats one-shot; ≥2 distinct providers correlates with winning (r=0.42); no single provider is consistently superior. Retrieve primary sources the crowd skims — the statute, docket, filing, launch manifest, calendar, changelog.
  - **Price-blindness enforcement lives here, mechanically:** an SDK `PreToolUse` hook denies any `WebSearch`/`WebFetch` call touching blocklisted domains (kalshi.com, polymarket.com, manifold.markets, electionbettingodds, aggregator/odds sites), and a `PostToolUse` hook scans fetched content for price-like mentions of this market; hits set the `contaminated` flag in the log. The forecaster cannot see a price even if it tries.
  - **Retrieval health is a first-class failure surface:** assert non-empty results, log every source, and cap confidence when retrieval is thin — one silently-failed feed produced fully hallucinated forecasts in a documented build (D.6).
- **Resolution-criteria interpretation pass (new, first-class).** Before forecasting: restate the resolution rule in own words, name the authoritative source, enumerate edge cases (timezone, "by" vs "before", what happens on a technicality), state the default resolution if nothing changes by the deadline, and check the question isn't already effectively resolved. Misreading criteria/status is the top documented LLM-forecaster failure mode — and the fine print is also where the edge thesis lives, so this pass is both defense and offense. Log its output separately so interpretation errors are auditable apart from forecasting errors.
- **Reasoning decomposition:** rephrase question → **explicit base rate / outside view** (winners compute base rates 40% vs 7% of losers) → inside view from retrieved news → pro/con weighing → probability. Batch logically related questions into one call for scope-coherence (bot medians otherwise violate probability sums by ~24%).
- **Ensemble: 6–8 samples across 2–3 model families, trimmed mean.** 86% of tournament winners ensemble; cross-family decorrelation beats more samples of one model; same-model multi-persona ensembles showed little gain. Claude members run as isolated SDK sessions (genuine decorrelation — no shared context); non-Claude members run behind the same `forecast()` contract via plain API calls, since the SDK drives Claude models only. Log per-member forecasts and the spread.
- **Cap outputs to ~[0.03, 0.97]** (capping correlated r=+0.48 with winning; one confident wrong forecast can erase a season). Note the tension: in longshot-heavy niches some edge lives outside the cap — revisit per-category once calibration data exists, but start capped.
- **Expect under-extremeness, not overconfidence.** The documented aggregate miscalibration of LLM forecasters is hugging 50% (bots' yes/no separation ~21pp vs pros' 36pp). The eventual recalibration direction is *extremizing* — see §3.
- **Skip fine-tuning.** Measured gain small (~0.007 Brier); only 1 of 13 tournament winners fine-tuned; RL fine-tunes of open models reach prior-generation parity only.
- **Format validation everywhere.** Units bugs and missed questions caused the largest single documented losses. Validate the output contract mechanically.
- **Output contract:** `{p, rationale, sources, criteria_interpretation, ensemble_members, retrieval_health}`.
- **Cost envelope:** winners' operating point is ~20–30 LLM calls ≈ $1–1.50/question; a serious quarter costs low hundreds to ~$2k, not $100k (D.6). **Billing note (v2.1):** the Agent SDK requires `ANTHROPIC_API_KEY` — subscription auth is not supported for SDK automation — so automated harness runs are metered API. Cap each member with `max_budget_usd`, log `total_cost_usd` per member, and treat ensemble size as the cost knob. Interactive development still happens in Claude Code on subscription; only the automated harness bills the API.

**Improvement loop.** Changes are versioned, but compared by **concurrent paired A/B on the same questions** (§3) — not sequential clock-resets: prompt/decomposition wording; ensemble size and family mix; research source mix; post-hoc recalibration (validated by replay, §3).

**Pitfalls.** Price leakage through research (the cardinal sin, now with a named enforcement mechanism). Silent retrieval failure → hallucinated forecast. Misread criteria. Outputs at 0/1. Research that's expensive but doesn't beat the no-research baseline (check via 2.9).

**Observe.** `p` distribution (hugging 0.5 = the documented failure mode); ensemble spread; retrieval health rate; contamination-flag rate; cost per forecast; per-version calibration and log score.

### 2.4 Decision & sizing layer

**Purpose.** Convert `(p, q, book, fees)` into an action. This is where "only act where you disagree enough" lives — and, per the evidence, *how* you act matters as much as whether.

**Build.** (See `decide()` in Appendix B.)
- **Maker-first posture** (D.4). Kalshi data: makers −9.6% vs takers −31.5% average returns; makers on ≥50¢ contracts +2.6% (the only positive bucket). Default action on sufficient edge: **post a limit order at your fair value ± required edge on the favorite side**, cancel-on-update, never rest through scheduled information events. Take only when the edge is large and the book is deep.
- **Edge thresholds, net of costs (evidence-based, in probability points):**
  - Maker: ≥ 1–1.5 pts past posted price after maker fee, plus a toxicity buffer (grown if markouts show adverse selection).
  - Taker in a liquid market (1–2¢ spread): ≥ 2.5–3 pts.
  - Taker in a thin market (5–15¢ spread): ≥ 5–9 pts — which mostly means don't take in thin markets.
  - Sub-10¢ longshot buys as taker: effectively never (market's own base rate for these trades is −60%).
- **Sizing: fractional Kelly on shrunk probabilities.** Shrink `p` toward `q` in proportion to your calibration uncertainty (the market price is a strong prior — D.2's AIA result: ensemble-with-market beats either alone), then apply 0.25–0.5 × Kelly **computed against the expected fill price, not the mid**. Rationale for deep fractionalization: overbetting is asymmetrically bad, and you trade exactly when your model most disagrees with the market — i.e., when it's most likely to be wrong (selection effect, D.4).
- **Risk structure** (borrowed from the reference market-making bot): per-market notional cap, per-cluster (event-group) worst-case ceiling, portfolio ceiling, daily-loss kill switch.
- Where thresholds aren't met, **pass** — the correct, most-common action. A practitioner weather bot that survived contact with reality rejects ~9/10 candidates (D.4).
- In sim, "action" = write the intended order (side, price, maker/taker, size) to the log; the fill simulator (2.5) determines what happens to it.

**Improvement loop.** Decision-layer parameters (thresholds, Kelly fraction, shrinkage weight) are pure functions of logged data — tune them by **replay on the logged record**, no new forecasts needed, then bump the decision-version. If you're passing on everything, fix the forecaster or the funnel; never loosen thresholds to get action.

**Pitfalls.** Over-Kelly. Taking in thin markets because the maker order didn't fill (unfilled is information: the market didn't come to you). Sizing off `q` instead of the executable price. Ignoring per-series fee params.

**Observe.** Trade rate; maker-vs-taker mix; edge distribution of taken vs passed; fill rate on maker orders; realized vs theoretical stakes; kill-switch state.

### 2.5 Paper execution / fill simulator

**Purpose.** Produce fills realistic enough that paper results predict live results. The evidence says this is where fake edges are born — criticality upgraded accordingly. Later, the seam where real execution slots in.

**Build.** (See `sim_fill()` in Appendix B; requirements from D.4.)
- **Taker orders:** walk the snapshotted book level by level, fees per level from the venue's per-series params; cap simulated size at ≤25% of displayed depth and a fraction of recent daily volume; partial fills are the default.
- **Maker orders — trade-through logic, not touch logic:** a resting order fills only when the market *trades through* your price, and only up to the volume that actually printed at-or-through. Touch-based fills are fantasy in thin books.
- **Adverse-selection haircut:** asymmetric fill probability — assume ~100% filled when the market subsequently moves against you, ~30–50% when it moves your way.
- **Staleness haircut:** evaluate the entry against the book N seconds/minutes after signal time, not at signal time (a documented live failure mode was signal staleness, not model accuracy).
- **Markout logging from day one:** for every simulated (and eventually real) fill, record mid at t+5min and t+1hr. Systematically negative markouts = your flow is toxic-selected; widen the toxicity buffer in 2.4.
- Maintain a paper portfolio: open positions, realized/unrealized paper P&L. Treat thin-market positions as hold-to-resolution (depth *shrinks* near resolution; a round-trip exit costs a second spread + fee).
- Kalshi's demo environment is for plumbing tests only — its liquidity is explicitly unrepresentative.

**Improvement loop.** If/when any real fills exist, reconcile real vs simulated and recalibrate the haircuts. Until then, hold the prior that live runs 20–50% below sim, and let the Phase-3 gate absorb it.

**Pitfalls.** Mid-price fills. Touch-fills on maker orders. Infinite size at top-of-book. Trade-direction labels inferred from public feeds are ~coin-flip noise on Polymarket — use actual fills/prints, not inferred flow.

**Observe.** Simulated slippage distribution; maker fill rate; frequency of size-capped fills; markout distribution.

### 2.6 Append-only forecast log — the scientific artifact

**Purpose.** The immutable record everything else reads from. The single easiest thing to under-build, and the thing the whole experiment's credibility rests on.

**Build.** One row per forecast (schema in Appendix B). Capture at forecast time: timestamp, venue, market id, question, category/vertical, **cluster_id**, `p`, **`q_mid`, `bid`, `ask` at book-snapshot time and again at decision time**, edge, side, **maker/taker intent**, order price, stake, simulated fill, fee params used, **harness version + decision version**, rationale, sources, criteria-interpretation output, **ensemble member forecasts + spread**, **retrieval-health and contamination flags**, `resolve_by`, and both **baseline `p`s** (market; no-research call).
- **Immutability rule:** forecast-time fields never change. Settlement (2.7) is the *only* later write, and it may touch only `outcome`, `resolved_at`, `resolution_note`, `disputed`.
- Store in SQLite/DuckDB so the dashboard can query it directly.

**Improvement loop.** Add columns as you discover slices you want — never rewrite history; new columns are NULL for old rows.

**Pitfalls.** Logging only one `q` (you need mid *and* touch, snapshot-time *and* decision-time — research takes minutes and the gap is silent look-ahead bias). Skipping cluster_id (uncorrectable later). Overwriting forecast-time fields. Not recording versions or fee params.

**Observe.** Row counts (total / open / resolved); write failures; schema-version drift.

### 2.7 Settlement tracker

**Purpose.** Come back days-to-weeks later, record the real `outcome` against each open forecast, and close the loop. Nothing gets scored until this runs.

**Build.**
- Poll each open market's resolution via the connector once past `resolve_by`. Kalshi settles within hours (up to ~48h for human-verified); Polymarket has a 2h dispute window minimum, and disputes escalate to token votes taking days-to-weeks.
- Write `outcome ∈ {0,1}`, `resolved_at`, `resolution_note`, `disputed`.
- **Flag ambiguous/disputed resolutions** for manual review rather than silently recording them. Track venue dispute signals (UMA status fields on Polymarket; Kalshi settlement revisions). Resolution surprises are data: they feed back into the resolution-clarity gate.
- Consider excluding disputed-resolution rows from the headline Δ_log (report both with and without) — a forecast that was "right on the facts, reversed by the resolver" measures resolution risk, not forecasting skill; you want both numbers visible.

**Improvement loop.** Improve ambiguity detection (e.g., flag when late price was far from final resolution). Track your own resolution-surprise rate by category — it's an input to the clarity gate.

**Pitfalls.** Never running it. Auto-recording ambiguous resolutions as clean. Off-by-one on resolution dates.

**Observe.** Settlement queue with overdue flags; disputed/flagged count; median time-to-resolution; resolution-surprise rate.

### 2.8 Evaluation & reconciliation engine

**Purpose.** Turn resolved rows into the numbers that answer "is there an edge, and where?" — with uncertainty machinery that survives correlated events and continuous peeking. Definitions in Appendix A; code in Appendix B. **Build this before the bot** — inability to tell whether a change helped is the most-cited cause of project death (D.6).

**Build.** Compute, over resolved forecasts:
- **Brier** and **log** score (yours, the market's, both baselines').
- **Market delta `Δ_log`** (and `Δ_Brier`) — the headline. Positive = you beat the price. Report against `q_mid` (optimistic bound) and against the executable touch (pessimistic bound).
- **Cluster-aware uncertainty:** cluster bootstrap CI (resample clusters = underlying events, not rows) + **paired permutation test** (swap p↔q within clusters). Report N, **N_eff**, and the variance-inflation ratio κ̂ = σ̂²_d / (2·Δ̂_log). An i.i.d. bootstrap on correlated events understates the CI exactly where you're most exposed.
- **Always-valid confidence sequence** on running Δ_log for the dashboard (Appendix A) — so continuously watching the number doesn't p-hack the experiment. Decisions happen at the pre-registered N or when the sequence's lower bound crosses zero downward.
- **CLV-style secondary signal:** movement of the market from trade-time `q` toward your `p` by close/resolution (mean logit shift). ~10× less data-hungry than outcome scoring, but it's a test of "no edge vs the market," not an unbiased Δ_log estimate — a fast diagnostic, never the headline (D.5).
- **Calibration curve** with binomial error bars; **Murphy decomposition** (reliability/resolution/uncertainty) — the base-rate parroter is calibrated with zero resolution; under-extremeness (the documented LLM failure mode) shows up here as low resolution.
- **Everything segmented by category (shrunken, §3) and by harness version (paired, §3).**
- **Markout and fill-quality panels** from 2.5's logs.

**Improvement loop.** This engine drives every other loop. Add slices as questions arise (time-to-resolution, ensemble spread, source mix, maker-vs-taker).

**Pitfalls.** Global averages hiding the edge's location. Reading Δ_log > 0 at N_eff=15 as success. i.i.d. resampling of correlated rows. Naive per-category winners. Mixing versions into one number.

**Observe.** This component *is* most of the dashboard (§4).

### 2.9 Baselines (cross-cutting)

**Purpose.** Cheap insurance against fooling yourself. If the harness doesn't beat these, you've learned that for almost nothing.

**Build.** Log alongside every real forecast:
- **Market baseline:** `baseline_p = q` ("do nothing / trust the price").
- **No-research baseline:** a single one-shot call to the same frontier model with retrieval disabled — an SDK `query()` with `allowed_tools=[]` and the same output schema, so it differs from the harness in exactly one respect. **This is a serious contender, not a strawman** — base-model quality dominates scaffolding (D.2), so this baseline isolates exactly what the expensive retrieval+ensemble machinery adds.
Score both through the same evaluator.

**Improvement loop.** The baseline ablation (§3): the harness must beat both baselines on Δ_log, out of sample, by a margin that clears its own cost. If it only ties the market baseline, there is no tradeable edge. If it only ties the no-research baseline, delete the machinery and keep the model call.

**Pitfalls.** Skipping this and attributing noise to the pipeline.

**Observe.** Harness vs each baseline on the same panels, side by side.

### 2.10 Orchestration + pre-registration

**Purpose.** Run the loop on a cadence, and enforce version discipline so results stay honest.

**Build.**
- A runner that: ingest → select → forecast → decide → sim → log, on a schedule (or manually at first).
- A **frozen `config.yaml`** = your pre-registration: pinned harness version, vertical, thresholds, Kelly fraction, **minimum edge of interest and its target N (from the power table)**. Committing a change bumps a version.
- Reliability engineering is not overhead — "make sure the thing runs all the time" is the most-repeated advice from tournament survivors (D.6): retries, idempotent runs, loud failures, and a heartbeat the dashboard shows.
- The runner is plain Python (asyncio); the SDK appears only inside `forecast.py`. Automated harness runs bill the API key (the SDK doesn't support subscription auth), so the runner enforces a per-run budget from `config.yaml` on top of the per-member `max_budget_usd`, and halts loudly when it's hit.

**Improvement loop.** The outer scientific loop (§3) is what the runner + config make real.

**Pitfalls.** Tweaking mid-sample and counting the whole record. Cron-driven autonomy before the logic is stable. Silent non-runs (alert on missed heartbeats, §4).

**Observe.** Run status/history; last successful run; current pinned versions; cost/usage per run.

---

## 3. Improvement loops (the meta-process)

Two levels: the **outer scientific loop** that governs the whole thing, and **per-component loops** it drives. v2's central change: the v1 "change one thing → reset the clock" loop is statistically honest but *calendar-ruinous* (each honest sequential version costs months at realistic N — Appendix A). The evidence-preserving fix is to classify changes by how they can be validated:

### Three classes of change, three validation paths

1. **Forecaster changes (alter `p`): concurrent paired A/B.** Run old and new harness versions **in parallel on the same questions** and score the per-event *paired difference* between versions. Pairing kills event-level variance — distinguishing v_{n+1} from v_n needs far fewer events than distinguishing either from the market. Shadow-run the challenger; promote when the paired CI favors it; the promoted version's *market-facing* record still starts at its own first forecast.
2. **Decision-layer changes (thresholds, Kelly fraction, shrinkage, maker/taker posture): replay.** These are deterministic functions of logged `(p, q, book, fees)` — re-run them over the cached record. No new forecasts, no clock, no contamination. Version them separately from the harness (`decision_version`).
3. **Recalibration (post-hoc map on `p`): forward-chaining cross-validation on history.** Folds split by resolution date, clusters intact, train-on-past/test-on-future. Accept only if it improves out-of-fold log score consistently. Then either freeze it before the next evaluation window or run it prequentially (each forecast uses only data resolved before its issue time). Method: **one-parameter extremizing (temperature in logit space) from ~50–100 resolutions, Platt from ~100–300. Isotonic needs ~1,000+ — skip it** (D.5). Expected direction: extremizing (LLMs are under-extreme); expected gain: ~0.01–0.016 Brier (D.2).

**The iron rule, restated for v2:** forecasts made under one configuration never silently count toward another's record. Concurrent A/B and replay exist precisely so honesty stops costing months.

### The outer scientific loop

```
pre-register (freeze config: version, vertical, min edge Δ*, target N from power table)
      │
      v
run forward, log everything ──────────────┐
      │                                    │  (challenger versions shadow-run in parallel;
      v                                    │   decision tweaks validated by replay)
accumulate cluster-level resolutions       │
      │  <────────────────────────────────┘
      v
evaluate continuously via confidence sequence (peek freely — it's anytime-valid)
      │
      v
DECIDE only at pre-registered N, or if CS lower bound < 0 (clearly losing → stop early)
      │
      v
diagnose (shrunken per-category Δ, decomposition, baselines, markouts) ──> next version ──> (back to run)
```

### Overfitting guardrails

- Paired comparisons for versions; replay for decisions; forward-chaining CV for recalibration — never in-sample tuning reported as results.
- The confidence sequence is the only number you may watch continuously; the fixed-N CI is read once, at the pre-registered N.
- Per-category conclusions only through EB shrinkage + time-split confirmation (Appendix A). Never report a selected category's unshrunken in-sample Δ.
- Prefer changes with a mechanistic reason over blind prompt-fiddling — prompt gains plateau on frontier models anyway (D.2).

### Per-component loop summary

| Component | Signal that drives change | Validation path |
|-----------|---------------------------|-----------------|
| Selection (2.2) | shrunken Δ_log by category; throughput rate | time-split confirmation, then re-pre-register |
| Forecaster (2.3) | paired A/B vs incumbent | concurrent shadow-run |
| Recalibration | reliability diagram; forward-chained CV gain | prequential / frozen-window |
| Decision (2.4) | replayed P&L / Δ under alternative params; markouts | replay on logged record |
| Fill sim (2.5) | real-vs-sim gap (if any live); markout drift | reconciliation |
| Settlement (2.7) | resolution surprises, dispute rate by category | feeds clarity gate |

### When to conclude "no edge"

If, at the pre-registered N (counted in clusters) for a couple of honest configurations, Δ_log versus the market baseline has a confidence interval straddling zero across your categories — that is a *result*, not a failure, and it is the expected outcome: professionals with $100K and this same design are barely positive on Kalshi and negative on Polymarket paper (D.6). Learned cheaply, it's the experiment working. Switch niche once, or stop.

---

## 4. Observability — the web dashboard

### Philosophy

The dashboard exists to do four things, in priority order:
1. **Detect an edge with honest uncertainty** (foreground Δ_log with its **confidence sequence**, not raw P&L).
2. **Localize it** (by category — shrunken — and by version — paired).
3. **Catch pipeline bugs and silent failures** (settlement queue, retrieval health, run heartbeat, cost).
4. **Prevent self-deception** (baselines side-by-side; N, N_eff, κ̂ always visible).

Deliberately **de-emphasize cumulative paper P&L** — one noisy realized path inviting the wrong "am I up?" instinct. Show P&L small, show Δ_log big.

### Panels

1. **Headline — edge over the market.** Running Δ_log vs market baseline with the **anytime-valid confidence-sequence band**, plus plain language: *"N resolved (N_eff clusters) · Δ_log = X · CS [a, b] · decision at N* = Y · currently indistinguishable from zero."* Secondary line: Δ_log vs executable touch (pessimistic bound).
2. **CLV-style fast signal.** Mean logit movement of the market from your trade time toward your `p` — the early-warning diagnostic (labeled as such, not as proof).
3. **Calibration & decomposition.** Reliability diagram with binomial error bars; Murphy breakdown; explicit under/over-extremeness callout (expected direction: under-extreme). You vs market vs both baselines.
4. **By category & by version.** Shrunken per-category Δ (raw + shrunken shown together so the shrinkage is visible); paired version A/B panel on overlapping questions.
5. **Selection funnel.** Ingested → each gate → forecasted → traded, per run, with cost per stage and the cluster-throughput rate vs target.
6. **Execution quality.** Maker fill rate, slippage, size-cap frequency, **markout distribution** (the toxic-flow detector).
7. **Per-forecast explorer.** Searchable table with drill-down to rationale, sources, criteria-interpretation, ensemble spread, contamination/retrieval flags. Essential for the manual log review 66% of tournament winners do (D.6).
8. **Open positions & settlement queue.** Overdue flags; disputed/ambiguous count; resolution-surprise rate.
9. **Ops / health.** Run heartbeat, error log, API/subscription usage vs caps, fee-schedule change alerts.

### Alerts (push these; don't rely on remembering to look)

- Pipeline run failed / didn't run (heartbeat missed).
- Retrieval-health failure or contamination-flag spike.
- Positions past `resolve_by` unsettled; any dispute flag raised.
- Cost/usage spike or near subscription cap.
- Confidence-sequence lower bound crosses zero downward (clearly losing — early-stop condition).
- Markout drift beyond band (toxic flow).

### Stack (pragmatic → scale-up)

- **Data layer:** query the SQLite/DuckDB log directly.
- **Fastest to build:** Streamlit; or a small React app over a FastAPI/SQLite backend with recharts/plotly. Either is buildable in Claude Code as a single small app.
- **Compute stats server-side** with the Appendix B functions; the front end displays them.
- Scale-up (later, probably never): Grafana. Note on artifacts: if prototyping the dashboard as a browser artifact, don't use `localStorage`/`sessionStorage` — keep state in memory or read from the backend.

### Make observation possible at write-time

The dashboard can only show what you logged. The Appendix B schema is designed so every panel above is a query away — capture the dual `q`s, executable fill, versions, cluster_id, flags, ensemble spread, and baselines **at forecast time**, or the panels can't be built retroactively.

---

## 5. Phased roadmap

**Phase 0 — evaluator + plumbing + one vertical + baselines (paper).**
The evaluation engine and append-only log come *first* (the most-cited killer is not knowing whether anything helped). Then: Kalshi connector (with `mve_filter=exclude` and per-series fees), the selection funnel for one Tier-1 vertical (verify the throughput gate against real market flow before committing), settlement polling, and the market baseline (`p = q`). No harness yet. Goal: markets flow in, get filtered, logged, settled, and *scored end-to-end on the baseline*. Prove the loop closes.

**Phase 1 — harness + decision + sim (paper, forward).**
Blind forecaster on the Agent SDK (§1 implementation map; `forecasting-tools` only for tournament plumbing and non-Claude members), with the no-research baseline logged alongside, criteria-interpretation pass, decision layer (maker-first), trade-through fill sim with markout logging. Pre-register: minimum edge of interest Δ* (recommended 0.05 nats), target N from the power table, thresholds, Kelly fraction. Run forward.
*Optional but recommended:* enter the current Metaculus FutureEval season with the same harness — free scoring signal at zero capital risk, remembering its objective differs from trading EV.
*Gate:* pipeline runs unattended-reliably; first ~30–50 cluster resolutions in; scoring verified correct; retrieval-health and contamination rates acceptable.

**Phase 2 — dashboard + improvement loops.**
Build the dashboard (§4). Run the loops: shadow A/B for harness changes, replay for decision changes, recalibration once ≥~100 resolutions (expect to extremize), shrunken category feedback into selection. Accumulate toward the pre-registered N — realistically 300–500 effective clusters for Δ* = 0.05, i.e., **12–20 months at 25/month throughput; this is the honest price of the answer** (push throughput, not standards, to shorten it).
*Gate:* a defensible Δ_log with a cluster-aware CI at the pre-registered N, per category (shrunken), per version (paired), beating both baselines net of cost.

**Phase 3 — decision (only if edge is proven).**
If Δ_log beats both baselines out of sample with the CI clear of zero in some category — and only then — consider tiny real-money bets on **Kalshi** (legal end-to-end, maker-first, position interest), sized by fractional Kelly on shrunk probabilities, expecting: fills at ~40–50% of intents (FutureSearch's realized rate), live results 20–50% below sim, and capacity measured in hundreds of dollars per market. If not: switch niche once, or stop. Either outcome is a successful experiment. **Do not plan around trading profits** — every verified real-money forecasting-alpha result is negative, tiny-sample, or liquidity-capped (D.6); the defensible upside is the skill, the infrastructure, and the answer.

---

## 6. Pitfalls / anti-patterns checklist

Carried from v1, with evidence-driven additions marked •new•:

- [ ] **Price leakage** — forecaster sees `q` directly *or via retrieved articles quoting odds*. Blocklist + contamination flag. (Cardinal sin.)
- [ ] **Backtesting on resolved markets** — LLM contamination; every "superhuman" claim died of this; forward-only.
- [ ] **Scoring at mid, no fees** — manufactures fake edge; score executable, fees from per-series API params. •updated•
- [ ] •new• **Touch-fills on maker orders / taking in thin markets** — trade-through logic or nothing; takers of cheap contracts lose 30%+ structurally.
- [ ] •new• **Buying sub-10¢ longshots as taker** — the market's own base rate for this trade is −60%.
- [ ] •new• **Resting quotes through scheduled information events** — free option written to faster traders.
- [ ] **Outputs at 0/1** — clamp; cap ~[0.03, 0.97] per tournament evidence.
- [ ] •new• **i.i.d. bootstrap over correlated events** — understates the CI exactly where exposure is worst; cluster by underlying event, report N_eff.
- [ ] •new• **Peeking at a fixed-N CI continuously** — inflates false positives to ~25–30%; watch the confidence sequence instead.
- [ ] •new• **Counting markets instead of clusters** toward the sample-size gate.
- [ ] •new• **Ignoring priced theta** — a long-dated "mispricing" must clear the ~5–8%/yr settlement discount before it's real.
- [ ] •new• **Silent retrieval failure** → hallucinated forecast; assert non-empty, log sources, cap confidence when thin.
- [ ] •new• **Format/units bugs** — the largest single documented losses; validate outputs mechanically.
- [ ] •new• **Transferring Manifold results to real money** — its exploitable features (default-50% pricing, benign counterparties) don't exist on real venues.
- [ ] **Counting pre-change forecasts for a new version** — use paired A/B and replay instead of silent pooling.
- [ ] **Chasing cumulative P&L** instead of Δ_log with honest uncertainty.
- [ ] **Reading small-N results as real**; **global averages** hiding the edge; **unshrunken per-category winners**. •updated•
- [ ] **Over-Kelly** — fractional, on shrunk probabilities, off fill price. •updated•
- [ ] **Loosening thresholds to "get action."**
- [ ] **Ignoring resolution risk** — fat tail, not nuisance; clarity gate + dispute flags; report Δ_log with and without disputed rows. •updated•
- [ ] **Chasing volume** (efficient markets).
- [ ] **Never running settlement.**
- [ ] **Mutating forecast-time log fields.**
- [ ] **Skipping baselines** — especially the no-research call, which the evidence says is a serious contender.

---

## Appendix A — the math

Two ingredients per forecast: your probability `p` and the outcome `o ∈ {0,1}`. The market's probability at the same instant is `q`.

**Brier score** (MSE on probabilities; 0–1, lower better; 0.25 = always saying 0.5):
```
BS = mean( (p − o)^2 )
```
Proper: minimized in expectation only by reporting true belief.

**Log score** (≤0, higher/closer-to-0 better):
```
LS = mean( o·ln(p) + (1−o)·ln(1−p) )
```
Unbounded below — hence the clamp. Negated, this is log loss / cross-entropy.

**Calibration / reliability diagram.** Bin forecasts by `p`; plot mean predicted vs observed frequency per bin, binomial error bars. Calibration is necessary but not sufficient — the base-rate parroter is calibrated and useless — hence:

**Murphy decomposition of Brier:**
```
BS = Reliability − Resolution + Uncertainty
Reliability = (1/N) Σ_k n_k (p̄_k − ō_k)^2      # want LOW
Resolution  = (1/N) Σ_k n_k (ō_k − ō)^2         # want HIGH — what the parroter lacks
Uncertainty = ō(1 − ō)                           # fixed by the world
```
The documented LLM failure mode (under-extremeness) appears here as low Resolution with fine Reliability.

**Edge and expected value.** Buying "yes" at price `q` under belief `p`: `EV = p − q = e`. Disagreement, expected profit per unit, and growth rate — one number.

**Fractional Kelly sizing** (binary, buying "yes" at executable price `x`, shrunk belief `p̃`):
```
full-Kelly stake fraction  f* = (p̃ − x) / (1 − x)          # "no" side: (x − p̃)/x
use   f = kelly_fraction · f*     with kelly_fraction ∈ [0.25, 0.5]
```
Use the *fill price*, not the mid. Shrink `p` toward `q` before sizing (see below). Why deeply fractional: overbetting is asymmetrically destructive (2× Kelly = zero growth; full Kelly has ~X% chance of ever losing X% of bankroll), your `p` is a noisy estimate, and you trade exactly when your model most disagrees with the market — the selection effect concentrates your bets where you're most likely wrong.

**Market delta — the headline.**
```
Δ_log = mean( [o·ln p + (1−o)·ln(1−p)] − [o·ln q + (1−o)·ln(1−q)] )   # >0 ⇒ you beat the market
```
With expectation under true probability `r`: `E[Δ_log] = KL(r‖q) − KL(r‖p)` — the market's distance from truth minus yours, and exactly a full-Kelly bettor's expected log-bankroll growth per bet. The dashboard foregrounds Δ_log because paper P&L is one noisy draw from this rate.

**Variance and power (the v2 correction).** The per-event difference `d = o·ln(p/q) + (1−o)·ln((1−p)/(1−q))` is a two-point random variable with exact moments:
```
E[d]   = KL(π‖q) − KL(π‖p)                      # π = true probability
Var(d) = π(1−π) · (logit p − logit q)^2
```
Small-disagreement expansion (`p = q + δ`): `Δ_log ≈ δ²/(2q(1−q))` and `SD(d) ≈ δ/√(q(1−q))`, giving the load-bearing identity:
```
Var(d) ≈ 2·Δ_log        # BEST case: calibrated forecaster, all disagreement is signal
```
Real forecasters carry noise in their disagreement; simulation shows the variance-inflation ratio κ = Var(d)/(2Δ_log) runs ~2.5–8 with no change in Δ. Sample size for a two-sided α=0.05 test:
```
N ≈ (z_{α/2} + z_β)² · Var(d) / Δ²  =  15.7/Δ_log at 80% power (κ=1 floor)
```

| Δ_log (nats) | ≈ disagreement at q=0.5 | N (80% power, κ=1) | N (κ≈3, realistic) |
|---|---|---|---|
| 0.01 | ~7 pp | 1,570 | ~4,700 |
| 0.03 | ~12 pp | 525 | ~1,600 |
| 0.05 | ~16 pp | 315 | ~950 |
| 0.10 | ~22 pp | 157 | ~470 |

After ~50 resolutions, replace the floor with the observed variance: `N = 7.85·σ̂_d²/Δ*²`. **Count clusters, not markets** (below). This table is why the roadmap pre-registers Δ* = 0.05 and targets hundreds of effective resolutions — and why throughput is a selection gate.

**Clustered events.** Markets resolving on the same underlying event are one draw. With mean cluster size m̄ and intra-cluster correlation ρ: `N_eff = N / (1 + (m̄−1)ρ)`; with near-duplicate markets (ρ→1), N_eff ≈ number of distinct events. All CIs by **cluster bootstrap** (resample clusters, keep members together); need ≥~30–40 clusters for trustworthy intervals (wild cluster bootstrap below that). Never split a cluster across CV folds.

**Always-valid confidence sequence** (asymptotic, Waudby-Smith–Ramdas form) on running mean d̄_t with running SD σ̂_t:
```
radius(t) = σ̂_t · sqrt( 2(tρ²+1)/(t²ρ²) · ln( sqrt(tρ²+1)/α ) )
CS_t = d̄_t ± radius(t)          # valid simultaneously for ALL t — peek freely
```
Pick ρ by minimizing radius at the planning horizon t* ≈ pre-registered N (one scipy line). Cost of anytime validity: ~1.5–2× wider than the fixed-N CI — budget ~2× the table's N if you insist on deciding from the CS alone; the cheaper hybrid is: decide at the pre-registered N, use the CS only for early-stopping when clearly losing.

**Empirical-Bayes shrinkage for per-category Δ** (the winner's-curse antidote — with K categories the best-looking one is inflated by ≈ its SE·√(2 ln K)):
```
Δ̂_k, se_k per category (cluster-bootstrap se)
μ̂   = precision-weighted grand mean
τ̂²  = max(0, Var_between(Δ̂_k) − mean(se_k²))
Δ̃_k = μ̂ + τ̂²/(τ̂² + se_k²) · (Δ̂_k − μ̂)          # allocate/select on THESE
```
When τ̂² = 0 (usual early), everything shrinks to the grand mean — the correct statement that categories aren't yet distinguishable.

**Recalibration data requirements:** 1-parameter extremizing `p′ = σ(a·logit p)` from ~50–100 resolutions (expect a ∈ [1.2, 2.5] — extremize, since LLMs are under-extreme); Platt `p′ = σ(a·logit p + b)` from ~100–300; isotonic ~1,000+ (skip). Validate by forward-chaining CV, clusters intact.

**Theta hurdle:** prices embed an annualized settlement discount ≈ 3–7% for locked capital (roughly halved on Kalshi by its ~4% APY on positions). A candidate edge on a T-day market must exceed `hurdle ≈ (0.03–0.07) · T/365` (in expected-return terms) plus fees before it counts as mispricing.

---

## Appendix B — schema + core code

### SQLite schema (the append-only log)

```sql
CREATE TABLE forecasts (
  id                INTEGER PRIMARY KEY,
  ts_forecast       TEXT NOT NULL,      -- ISO8601, when p was made
  venue             TEXT,
  market_id         TEXT,
  question          TEXT,
  category          TEXT,               -- for segmentation
  vertical          TEXT,
  cluster_id        TEXT NOT NULL,      -- underlying real-world event (evaluation unit)
  p                 REAL NOT NULL,      -- your probability (capped)
  q_mid_snap        REAL NOT NULL,      -- market mid at book-snapshot time
  bid_snap          REAL, ask_snap      REAL,
  q_mid_decide      REAL,               -- market mid at decision time (research takes minutes)
  bid_decide        REAL, ask_decide    REAL,
  edge              REAL,               -- p - q_mid_decide
  side              TEXT,               -- 'yes' | 'no' | 'pass'
  order_type        TEXT,               -- 'maker' | 'taker' | NULL
  order_price       REAL,               -- limit price posted (maker) or NULL
  stake             REAL,               -- sim units / bankroll fraction
  fill_price        REAL,               -- EXECUTABLE simulated fill (NULL if maker unfilled)
  fill_qty_frac     REAL,               -- fraction of intended size filled
  fee_params        TEXT,               -- JSON: venue fee formula params AT TRADE TIME
  harness_version   TEXT NOT NULL,
  decision_version  TEXT NOT NULL,
  rationale         TEXT,
  sources           TEXT,               -- JSON list
  criteria_interp   TEXT,               -- resolution-criteria interpretation pass output
  ensemble_members  TEXT,               -- JSON list of member p's
  ensemble_spread   REAL,
  retrieval_ok      INTEGER,            -- retrieval-health assertion result
  contaminated      INTEGER,            -- price-leak scan hit
  baseline_market_p REAL,               -- = q (trust-the-price baseline)
  baseline_norsrch_p REAL,              -- one-shot no-research frontier call
  resolve_by        TEXT,
  markout_5m        REAL, markout_1h    REAL,   -- mid moves after fill (fill-quality)
  resolved_at       TEXT,               -- settlement ONLY
  outcome           INTEGER,            -- 1|0, NULL until resolved (settlement ONLY)
  disputed          INTEGER,            -- resolution was contested (settlement ONLY)
  resolution_note   TEXT                -- settlement ONLY
);
-- Immutability: after insert, only settlement may write resolved_at, outcome, disputed, resolution_note.
```

### Core scoring (minimal, complete)

```python
import numpy as np
EPS = 0.03                              # tournament-evidence cap; revisit per-category later
clamp = lambda p: min(1 - EPS, max(EPS, p))

def brier(p, o):
    return np.mean((np.asarray(p) - np.asarray(o)) ** 2)

def log_score(p, o):
    p = np.clip(p, 0.01, 0.99); o = np.asarray(o)
    return np.mean(o * np.log(p) + (1 - o) * np.log(1 - p))

def delta_log(p, q, o):                 # >0 => you beat the market
    return log_score(p, o) - log_score(q, o)

def calibration(p, o, bins=10):
    p, o = np.asarray(p), np.asarray(o)
    idx = np.clip(np.digitize(p, np.linspace(0, 1, bins + 1)) - 1, 0, bins - 1)
    return [(p[idx == b].mean(), o[idx == b].mean(), int((idx == b).sum()))
            for b in range(bins) if (idx == b).any()]

def murphy(p, o, bins=10):              # BS = rel - res + unc
    p, o = np.asarray(p), np.asarray(o); N = len(o); ob = o.mean()
    idx = np.clip(np.digitize(p, np.linspace(0, 1, bins + 1)) - 1, 0, bins - 1)
    rel = res = 0.0
    for b in range(bins):
        m = idx == b; n = m.sum()
        if n:
            rel += n * (p[m].mean() - o[m].mean()) ** 2
            res += n * (o[m].mean() - ob) ** 2
    return dict(reliability=rel / N, resolution=res / N, uncertainty=ob * (1 - ob))

def cluster_bootstrap_ci(p, q, o, clusters, n=10000, a=0.05):
    """CI on delta_log resampling CLUSTERS (underlying events), not rows."""
    p, q, o = map(np.asarray, (p, q, o)); clusters = np.asarray(clusters)
    uniq = np.unique(clusters); k = len(uniq)
    members = {c: np.flatnonzero(clusters == c) for c in uniq}
    s = []
    for _ in range(n):
        i = np.concatenate([members[c] for c in uniq[np.random.randint(0, k, k)]])
        s.append(delta_log(p[i], q[i], o[i]))
    return tuple(np.quantile(s, [a / 2, 1 - a / 2]))

def paired_permutation_p(p, q, o, clusters, n=10000):
    """Two-sided p-value: swap p<->q per CLUSTER under the exchangeable null."""
    p, q, o = map(np.asarray, (p, q, o)); clusters = np.asarray(clusters)
    obs = delta_log(p, q, o); uniq = np.unique(clusters)
    hits = 0
    for _ in range(n):
        swap = np.isin(clusters, uniq[np.random.rand(len(uniq)) < 0.5])
        pp, qq = np.where(swap, q, p), np.where(swap, p, q)
        hits += abs(delta_log(pp, qq, o)) >= abs(obs)
    return hits / n

def confidence_sequence(d, rho, alpha=0.05):
    """Anytime-valid CS on the running mean of per-event deltas d (see Appendix A).
    rho: minimize radius at your pre-registered horizon t*."""
    d = np.asarray(d); t = np.arange(1, len(d) + 1)
    mean = np.cumsum(d) / t
    sd = np.sqrt(np.maximum(np.cumsum(d**2)/t - mean**2, 1e-12))
    rad = sd * np.sqrt(2*(t*rho**2 + 1)/(t**2 * rho**2)
                       * np.log(np.sqrt(t*rho**2 + 1)/alpha))
    return mean, mean - rad, mean + rad

def eb_shrink(deltas, ses):
    """Empirical-Bayes shrinkage of per-category deltas; allocate on the result."""
    deltas, ses = np.asarray(deltas), np.asarray(ses)
    w = 1/ses**2; mu = np.sum(w*deltas)/np.sum(w)
    tau2 = max(0.0, np.var(deltas, ddof=1) - np.mean(ses**2))
    return mu + tau2/(tau2 + ses**2) * (deltas - mu)
```

### Forecaster harness (Claude Agent SDK; blind to q)

```python
import asyncio
from pydantic import BaseModel
from claude_agent_sdk import (query, ClaudeAgentOptions, ResultMessage,
                              HookMatcher, tool, create_sdk_mcp_server)

class Forecast(BaseModel):
    p: float                       # (0,1), pre-cap
    rationale: str
    sources: list[str]
    criteria_interp: str           # resolution-criteria interpretation pass (§2.3)

BLOCKED = ("kalshi", "polymarket", "manifold", "electionbettingodds",
           "predictit", "betfair", "metaculus", "odds")

async def deny_price_domains(input_data, tool_use_id, context):
    """PreToolUse hook: mechanical price-blindness (the cardinal-sin guard)."""
    ti = input_data.get("tool_input", {})
    text = (ti.get("query") or ti.get("url") or "").lower()
    if any(d in text for d in BLOCKED):
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "price-blindness blocklist"}}
    return {}

@tool("get_resolution_criteria",
      "Official resolution criteria text for a market", {"market_id": str})
async def get_criteria(args):
    text = load_cached_criteria(args["market_id"])   # from data/raw; never price fields
    return {"content": [{"type": "text", "text": text}]}

internal = create_sdk_mcp_server(name="marketdb", version="1.0.0",
                                 tools=[get_criteria])

def member_options(model):
    return ClaudeAgentOptions(
        model=model,                        # "opus" | "sonnet" | ... (Claude only)
        allowed_tools=["WebSearch", "WebFetch",
                       "mcp__marketdb__get_resolution_criteria"],
        permission_mode="dontAsk",          # headless, no permission prompts
        max_turns=25, max_budget_usd=0.40,  # runaway guards
        hooks={"PreToolUse": [HookMatcher(matcher="WebSearch|WebFetch",
                                          hooks=[deny_price_domains])]},
        mcp_servers={"marketdb": internal},
        output_format={"type": "json_schema",
                       "schema": Forecast.model_json_schema()},
        system_prompt=FORECASTER_PROMPT,    # decomposition recipe from §2.3
    )

async def forecast_member(question_prompt, model):
    async for msg in query(prompt=question_prompt, options=member_options(model)):
        if isinstance(msg, ResultMessage):
            if msg.subtype == "success" and msg.structured_output:
                f = Forecast.model_validate(msg.structured_output)
                return f, msg.total_cost_usd
            return None, msg.total_cost_usd  # format-failed member: drop + log

async def forecast(question, resolution_criteria, context):
    """Ensemble: isolated SDK sessions (Claude members) + non-Claude members via
    plain API calls behind the same contract. Trimmed mean + cap + retrieval_ok /
    contaminated flags applied in the aggregator. MUST NOT receive q."""
    prompt = render_prompt(question, resolution_criteria, context)   # never q
    claude = [forecast_member(prompt, m) for m in ("opus", "opus", "sonnet")]
    others = [forecast_via_api(prompt, m) for m in OTHER_FAMILY_MODELS]
    members = await asyncio.gather(*claude, *others)
    return aggregate_trimmed(members)
```

### Decision + fill sim (maker-first)

```python
def decide(p, q_mid, bid, ask, fee_pts, kelly=0.25, bankroll=1.0, shrink=0.5):
    """Maker-first. fee_pts = fee at relevant price in probability points
    (pull per-series from venue API). shrink: weight toward market prior."""
    pt = shrink * q_mid + (1 - shrink) * p          # shrunk belief
    MAKER_EDGE, TAKER_EDGE = 0.015, 0.03            # evidence-based floors (§2.4)
    if pt > ask + TAKER_EDGE + fee_pts:             # big edge: take the ask
        x = ask
        return ('yes', 'taker', x, kelly * (pt - x) / (1 - x) * bankroll)
    if pt < bid - TAKER_EDGE - fee_pts:             # big edge: hit the bid (buy NO)
        x = bid
        return ('no', 'taker', x, kelly * (x - pt) / x * bankroll)
    if pt - MAKER_EDGE - fee_pts > bid:             # post inside: bid at fair - edge
        x = pt - MAKER_EDGE - fee_pts
        if x > bid:                                 # improves the book, below fair
            return ('yes', 'maker', x, kelly * (pt - x) / (1 - x) * bankroll)
    if pt + MAKER_EDGE + fee_pts < ask:
        x = pt + MAKER_EDGE + fee_pts
        if x < ask:
            return ('no', 'maker', x, kelly * (x - pt) / x * bankroll)
    return ('pass', None, None, 0.0)
    # Callers enforce: side-of-bias gate (no sub-10c longshot taking), per-market /
    # per-cluster / portfolio caps, kill switch, cancel-on-update, no resting
    # through scheduled events.

def sim_fill_taker(book, side, size, depth_cap=0.25):
    """Walk the book; cap at depth_cap of displayed depth; partial fills default."""
    levels = book['asks'] if side == 'yes' else book['bids']
    avail = sum(q for _, q in levels) * depth_cap
    size = min(size, avail)
    filled, cost = 0.0, 0.0
    for price, qty in levels:
        take = min(qty * depth_cap, size - filled); cost += take * price; filled += take
        if filled >= size: break
    return (cost / filled, filled) if filled else (None, 0.0)

def sim_fill_maker(order_price, side, size, prints_after, market_drift):
    """Fill ONLY on trade-through: volume that printed at-or-through our price.
    Adverse-selection haircut: full fill when market moved against us, partial when
    it moved our way."""
    through = sum(q for px, q in prints_after
                  if (px <= order_price) == (side == 'yes'))
    adverse = (market_drift < 0) == (side == 'yes')   # drift toward our side = bad fill
    haircut = 1.0 if adverse else 0.4
    return min(size, through * haircut)
```

---

## Appendix C — resources

**Research anchors.** Halawi et al., *Approaching Human-Level Forecasting with Language Models* (arXiv 2402.18563) — retrieval mattered most (ablations); honest headline: *approached*, did not beat, the crowd. Paleka et al., *Pitfalls in Evaluating Language Model Forecasters* (arXiv 2506.00723) — why backtests lie. Bürgi, Deng & Whelan, *Makers and Takers* (Kalshi microstructure; the maker/taker and longshot numbers). Tetlock 2008 (liquidity ≠ efficiency). *When Certainty Is Not Worth It* (arXiv 2605.31431) — priced settlement discounts. Niculescu-Mizil & Caruana 2005 (recalibration sample sizes). Cameron & Miller 2015 (cluster inference). Waudby-Smith/Ramdas line (anytime-valid inference).

**Build / community.** Claude Agent SDK docs (code.claude.com/docs/en/agent-sdk — the harness runtime: python API, hooks, custom tools/MCP, structured outputs, sessions, subagents, cost tracking). Metaculus `forecasting-tools` + `metac-bot-template` (tournament plumbing + non-Claude member wrapper; actively maintained). Metaculus AIB/FutureEval quarterly postmortems on LessWrong (the richest technique evidence). FutureSearch's Kalshi trader case study + live dashboard (the closest existing operation to this design — study their fill rates). faintsignals.substack.com "Building an AI Prediction Bot" (honest solo build log; note it stalled on exactly the iteration-signal problem §3 solves). Sempere, *AI Forecasting in 2026: What 11 Analyses Say* (synthesis).

**Critical writing.** Nuño Sempere's incentive/alignment critiques of forecasting platforms (arXiv 2106.11248) — why tournament rank ≠ trading EV. Halawi's *Contra papers claiming superhuman AI forecasting*.

**Trader side.** Domer interviews (unfamiliar markets, escalating research, avoid disputable markets "like the plague"). Prophet Arena (live LLM-vs-Kalshi-price leaderboard — the honest benchmark for this project's ambition).

**Skip:** AI-generated "prediction bets" tip content; copy-trading/whale-following; the archived Polymarket `agents` repo and its fork ecosystem; viral "AI turned $1k into $14k" threads (unreproducible, contradicted by benchmarks).

---

## Appendix D — the evidence base

Distilled findings from the 2026-07-27 six-track research review. Confidence: [V] verified from primary source or live API probe; [S] secondhand/converging secondaries; [J] judgment call on thin evidence.

### D.1 Venues & APIs

- Kalshi: public REST market data without auth; full-depth books; inline rules text; candlestick history; official demo env; ~20 reads/s basic tier; WS needs API-key auth. **`mve_filter=exclude` mandatory** — 99.3% of unfiltered "markets" are parlay legs (400k → 66.7k real). 78.5% of quoted open markets: zero 24h volume; yes-side spread quartiles 1¢/5¢/11¢ (p90 one-sided). API migrating cents → dollar-strings; deprecated fields return 0. ~4% APY on cash *and open positions*. Fees per-series via API; taker ≈ round-up(0.07·C·P·(1−P)), maker $0 on most series. CFTC-regulated, bots officially supported; sports contracts under state-level litigation. [V, live-probed]
- Polymarket intl: US trading ToS-prohibited (close-only from US IPs); read-only data unblocked. Gamma = metadata (criteria inline; stale prices; limit silently capped at 100; keyset pagination required past offset 5,000; numbers as JSON-in-strings); CLOB = live full-depth books; public WS. Fee V2 (Mar 2026): taker C·rate·p(1−p), rate 0.04–0.07 by category, geopolitics 0; makers free + rebates. Polymarket US (CFTC): taker θ=0.06, maker rebate θ=−0.0125, KYC. UMA disputes: 1,150+ in 2026 YTD. [V]
- Manifold: play-money AMM (no books), creator-resolved, bots explicitly welcome, reads unauthenticated, 500 req/min. Sweepstakes program dead (Mar 2025). Integration testbed only. [V]

### D.2 LLM forecasting — what's proven

- Halawi et al.: Brier 0.179 vs crowd 0.149 on 914 forward questions — approached, didn't beat. Ablations: retrieval ~0.027 Brier (biggest), fine-tuning ~0.007. System was under-extreme. [V]
- Metaculus AIB (Q3'24→Fall'25): Pros beat bot teams every quarter (−8.9 to −20 head-to-head; gap not closing as question mix hardened); bots beat the *general* crowd (top few % of humans). [V]
- Winner techniques: 86% ensemble (6–8 samples, 2–3 model families, trimmed mean/middle-6); agentic multi-provider search (removal degrades ~3.6×; ≥2 sources r=0.42; no provider consistently best); explicit base rates (40% of top-15 vs 7% of bottom); prediction capping (r=+0.48); Platt scaling (+0.016 Brier, p<0.001); ~28 calls ≈ $1.40/question. Base model dominates: plain template on o3 placed 2nd of ~96 in Q2'25; prompting can't rescue weak models; scaffolding worth ~5–11 pts (~9 months of model progress). [V]
- Failure modes: under-extremeness (hug 50%; yes/no separation 21pp vs pros' 36pp); scope insensitivity (nested probabilities sum to 1.24×); misreading resolution criteria/status; format/units bugs (single largest losses); anchoring to provided medians; silent retrieval failure → hallucinated forecasts. [V]
- Vs real-money prices: Prophet Arena — most LLMs below market consensus; AIA Forecaster (Bridgewater) matches superforecasters but *underperforms liquid market consensus* (though ensemble(AIA+price) beats price alone); PolyBench — 5 of 7 frontier models lost money live. **No credible published system beats liquid real-money prices.** [V]
- Fine-tuning: skip (tiny gain; 1/13 winners; open-model RL tunes reach prior-gen parity only). `forecasting-tools` actively maintained (v0.2.92, May 2026). [V]

### D.3 Where edges exist

- Platform calibration is good in aggregate and is produced by a small sharp cohort: ~3% of Polymarket traders drive price discovery; only ~12% of top winners show robust skill under randomization (LBS/Yale, 1.7M accounts). Where they're absent, prices are whoever wandered in. [V/S]
- Tetlock 2008: liquidity does not imply efficiency — high-volume markets can be *more* biased. Thin markets: worse prices, tiny capacity (median combinatorial-arb episode executable for ~15 shares). The thesis survives as breadth, not depth. [V]
- Kalshi favorite-longshot bias: sub-10¢ buyers lose >60%; >70¢ contracts small *positive* post-fee returns; Fed/CPI markets near-perfectly calibrated (no edge). [V]
- Most-attested repeatable edge: rules-text literacy ("traders read the title, not the criteria") — procedural/legislative/regulatory/corporate-timing categories; then awards/pop-culture (worst calibration [S]); science/space timing. Domer's process: unfamiliar markets, escalating research, size ∝ confidence, avoid disputable markets "like the plague." [V/S]
- Resolution risk: UMA token votes reversed correct positions ($7M Ukraine-minerals; $160M+ Zelenskyy-suit); top-10 UMA voters ~30–50% of vote weight; Kalshi failures are wording-ambiguity (Khamenei carveout; UFC scorecards), no oracle attack surface. [V]
- Priced theta: annualized settlement discount ~3–7%; 48–88% of long-horizon "miscalibration" is the discount, not bias. Near-certainties at long tenor ≈ Treasury yield with dispute tail. [V]

### D.4 Execution microstructure

- Kalshi (314k contracts, 2021–Apr'25): makers −9.6% vs takers −31.5% avg; makers on ≥50¢: +2.6% (only positive bucket). Median lifetime volume per (filtered!) contract ~$9k; mean trade $100. [V]
- Polymarket tick study (30B events): median half-spreads ~400bps at mid-range prices, 1,300–1,800bps sub-10¢; depth near-uniform across levels (thin touch); depth *decays* toward resolution; ~32 makers/market median. Realistic fillable size in niche markets: $100–$1,000. [V]
- Feed-inferred trade direction ≈ 59% accurate on Polymarket — don't build flow analytics from public-feed labels. [V]
- Fees in probability points (Kalshi taker): 1.75 at 50¢, 0.63 at 10/90¢ — but ~6.6% of stake per side at 5¢. Round-up-per-order penalizes small orders. [V]
- Sim best practices: trade-through fills for makers; adverse-selection asymmetric fill probability (~100% when wrong, 30–50% when right); staleness lag; ≤25% displayed depth; partial fills default; markouts on every fill; expect live 20–50% below sim. Practitioner weather bot: rejects ~9/10 candidates, $0.05 max-spread gate, killed by signal staleness not model quality. [V/S/J]
- Kelly: 25–50% fractional on shrunk p; overbetting asymmetrically bad; selection effect (you bet when most-divergent = most-likely-wrong) argues for deep fractionalization. [V/J]

### D.5 Evaluation statistics

- Exact: Var(d) = π(1−π)(logit p − logit q)²; best-case Var(d) ≈ 2Δ_log; N ≈ 15.7/Δ (80% power) as floor; κ ≈ 2.5–8 realistic inflation (Monte Carlo verified). Detecting 0.01 nats = thousands of events; 0.05–0.10 = realistic first-year range. [V-derivation]
- Pairing vs market (same events) = 5–25× variance reduction vs unpaired score comparison — never compare unpaired means. Market-as-control-variate collapses toward CLV: low-variance null test, biased for estimation — secondary signal only. [V-derivation]
- Cluster at the underlying event: N_eff = N/(1+(m̄−1)ρ); cluster bootstrap (≥30–40 clusters; wild bootstrap below); never split clusters across folds. [V]
- Peeking at fixed-N CIs: type-I inflates to ~25–30%+; fix = anytime-valid confidence sequences (~1.5–2× wider); hybrid recipe: decide at pre-registered N, CS for early stop. [V]
- Recalibration data needs: temperature ~50–100, Platt ~100–300, isotonic ~1,000+ (skip). Forward-chaining CV, prequential or frozen-window application. [V]
- Category selection: winner's curse ≈ SE·√(2 ln K); EB shrinkage + time-split confirmation. [V]

### D.6 Project postmortems

- Causes of death, ranked: (1) sample-size purgatory / no iteration signal (the faintsignals build stalled exactly here); (2) the edge was never forecasting — the documented $40M/yr Polymarket profit pool was latency arbitrage; real-money LLM-forecasting results are negative/tiny/capacity-limited; (3) ops fragility (silent feed failures, format bugs, cron flakiness); (4) liquidity — FutureSearch could deploy only ~$50K of a $100K Kalshi book, ~43% fill rate; (5) platform risk (Manifold's 10:1 devaluation; Polymarket archiving its `agents` repo with unresolved install bugs at 3.7k stars); (6) motivation decay — not API cost (~$0.50–$2/question; the $100k/quarter fear was off by ~100×). [V]
- FutureSearch Kalshi case study (closest existing operation to this doc): edge threshold >2% + honest book-walking; Kalshi real-money +$11.1K on $100K (tiny sample, mostly unrealized), Kalshi paper positive, **Polymarket paper negative** — liquidity the binding constraint throughout. [V dashboard]
- Sentinel: elite human forecasters remain the probability-assigning layer with LLM triage — the best track records chose the *opposite* architecture; nobody in this space lives off market edge (donation-funded, sub-$1M ops). [V]
- Sempere's incentive critiques: tournament scoring rewards extremizing/cherry-picking/median-copying — tournament rank and trading EV are different objectives; tune for one. [V]
- Manifold LLM-bot successes (e.g., 10× mana in 2 months) harvested default-50% pricing on fresh markets — a feature real venues don't have; don't transfer. [V]

---

*Build order, condensed: evaluator and log first, then close the loop on paper (ingest → select → log → settle) on Kalshi with the parlay filter on, add the blind harness on the maintained scaffold with the criteria-interpretation pass, decision layer maker-first, fill sim with trade-through logic — then run forward under a pre-registered minimum edge and sample size, watching the confidence sequence, shadow-A/B-ing every change. The number that decides everything is your log-score margin over the market, counted in clusters and read with cluster-aware uncertainty — and the honest prior, from everyone who has tried, is that it will be indistinguishable from zero. The system above is designed to make that answer cheap, fast, and trustworthy — and to be standing on the rare patch of ground (rules fine print, thin fast markets, maker side) where the evidence says a nonzero answer is least implausible.*
