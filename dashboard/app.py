"""Observability dashboard for the prediction-market forecasting bot (doc §4).

Philosophy (doc §4): Δ_log big, P&L small, uncertainty always visible.
Reads the append-only SQLite log (bot/log.py schema) directly; heavy statistics
come from bot.evaluate, which is imported LAZILY inside functions because it is
developed in parallel — every use is wrapped so the dashboard renders (with a
friendly notice) even when evaluate is absent or raising.

Run with:  streamlit run dashboard/app.py
DB path:   $BOT_DB_PATH  >  config.yaml run.db_path  >  data/forecasts.db
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "forecasts.db"
OVERDUE_GRACE_DAYS = 3      # resolve_by more than this many days past => overdue
HEARTBEAT_MAX_HOURS = 26    # no ok run for longer than this => heartbeat warning
DEFAULT_TARGET_N = 400      # fallback when config.yaml is unreadable

# Palette (dataviz reference instance): categorical slots + reserved status hues.
C_BLUE = "#2a78d6"      # series 1 — "you" / primary measure
C_BLUE_LT = "#9ec5f4"   # lighter step of the same hue (raw vs shrunk pairing)
C_ORANGE = "#eb6834"    # series 2 — market / maker-taker second series
C_AQUA = "#1baf7a"      # series 3
C_MUTED = "#898781"     # axis / reference ink
C_GRID = "#c3c2b7"      # baseline / 45-degree reference
C_GOOD = "#0ca30c"      # status: good (reserved)
C_CRIT = "#d03b3b"      # status: critical (reserved)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested; no streamlit calls, no lazy-module surprises)
# ---------------------------------------------------------------------------

def _num(x) -> float | None:
    """float(x) with None for None/NaN/unparseable."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def _fmt(x, nd: int = 4) -> str:
    v = _num(x)
    return "—" if v is None else f"{v:.{nd}f}"


def _parse_ts(s) -> datetime:
    """ISO-8601 (Z or offset) -> aware UTC datetime. Naive assumed UTC."""
    dt = s if isinstance(s, datetime) else datetime.fromisoformat(
        str(s).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _records(rows) -> list[dict]:
    if rows is None:
        return []
    if isinstance(rows, pd.DataFrame):
        return rows.to_dict("records")
    return list(rows)


def load_forecasts(con) -> pd.DataFrame:
    """All forecast rows, oldest first (ts_forecast order)."""
    return pd.read_sql_query(
        "SELECT * FROM forecasts ORDER BY ts_forecast, id", con)


def load_runs(con) -> pd.DataFrame:
    """All pipeline runs, newest first."""
    return pd.read_sql_query("SELECT * FROM runs ORDER BY id DESC", con)


def overdue_ids(rows, now: datetime) -> list:
    """Ids of open rows whose resolve_by is > OVERDUE_GRACE_DAYS before `now`.

    rows: DataFrame or iterable of dicts with 'id' and 'resolve_by'.
    Rows with missing/unparseable resolve_by are skipped.
    """
    out = []
    grace = timedelta(days=OVERDUE_GRACE_DAYS)
    for r in _records(rows):
        rb = r.get("resolve_by")
        if rb is None or (isinstance(rb, float) and rb != rb) or rb == "":
            continue
        try:
            due = _parse_ts(rb)
        except (ValueError, TypeError):
            continue
        if now - due > grace:
            out.append(r.get("id"))
    return out


def significance_line(overall, target_n) -> str:
    """Plain-language headline (doc §4 panel 1) from evaluate's overall dict.

    Every field may be None/missing (tiny N) — degrades to 'not yet significant'.
    """
    o = dict(overall or {})

    def g(k):
        return _num(o.get(k))

    n, n_eff = g("n"), g("n_eff")
    if n_eff is None:
        n_eff = g("n_clusters")
    dl, lo, hi = g("delta_log"), g("ci_lo"), g("ci_hi")

    if lo is not None and lo > 0:
        status = "currently significant (CS lower bound above zero)"
    elif hi is not None and hi < 0:
        status = "clearly negative (CS upper bound below zero — early-stop condition)"
    else:
        status = "not yet significant (CS straddles zero)"

    return " · ".join([
        f"{int(n) if n is not None else 0} resolved"
        f" ({int(n_eff) if n_eff is not None else '—'} clusters)",
        f"Δ_log = {_fmt(dl)}",
        f"CS [{_fmt(lo)}, {_fmt(hi)}]",
        f"decision at N* = {target_n}",
        status,
    ])


def latest_funnel(runs) -> list[tuple[str, float]]:
    """Per-gate survivor counts from the most recent run carrying funnel JSON.

    runs: DataFrame or list of dicts, NEWEST FIRST (load_runs order).
    Returns [(gate, count), ...] in the funnel's own (insertion) order.
    """
    for r in _records(runs):
        f = r.get("funnel")
        if f is None or (isinstance(f, float) and f != f):
            continue
        if isinstance(f, str):
            try:
                f = json.loads(f)
            except (ValueError, TypeError):
                continue
        if isinstance(f, dict) and f:
            return [(str(k), v) for k, v in f.items()]
    return []


def paper_pnl(rows) -> float:
    """Small, deliberately de-emphasized paper P&L (doc §4: show P&L small).

    Sum over resolved, filled, non-pass rows of
        ((payout - fill_price) - fee) * stake * fill_qty_frac
    where payout = outcome for 'yes' and 1-outcome for 'no' (fill_price is the
    price paid for the traded side; fee is in probability points).
    """
    total = 0.0
    for r in _records(rows):
        if r.get("side") not in ("yes", "no"):
            continue
        o, fp = _num(r.get("outcome")), _num(r.get("fill_price"))
        if o is None or fp is None:
            continue
        frac = _num(r.get("fill_qty_frac")) or 0.0
        stake = _num(r.get("stake")) or 0.0
        fee = _num(r.get("fee")) or 0.0
        payout = o if r.get("side") == "yes" else 1.0 - o
        total += ((payout - fp) - fee) * stake * frac
    return total


def headline_deltas(df: pd.DataFrame) -> pd.DataFrame:
    """Rows feeding the headline CS: resolved, non-contaminated, with p and a
    market baseline (baseline_market_p, falling back to q_mid_snap), ordered by
    ts_forecast. Returns columns ts_forecast, p, q, o."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["ts_forecast", "p", "q", "o"])
    d = df.copy()
    contaminated = pd.to_numeric(d.get("contaminated"), errors="coerce").fillna(0)
    q = pd.to_numeric(d.get("baseline_market_p"), errors="coerce")
    q = q.fillna(pd.to_numeric(d.get("q_mid_snap"), errors="coerce"))
    d = d.assign(q=q,
                 o=pd.to_numeric(d.get("outcome"), errors="coerce"),
                 p_=pd.to_numeric(d.get("p"), errors="coerce"))
    d = d[(contaminated != 1) & d["o"].notna() & d["q"].notna() & d["p_"].notna()]
    d = d.sort_values(["ts_forecast", "id"] if "id" in d else "ts_forecast")
    return pd.DataFrame({"ts_forecast": d["ts_forecast"].values,
                         "p": d["p_"].astype(float).values,
                         "q": d["q"].astype(float).values,
                         "o": d["o"].astype(float).values})


def running_cs(df: pd.DataFrame, rho: float) -> pd.DataFrame:
    """Running anytime-valid confidence sequence on per-event Δ_log.

    Uses bot.evaluate (lazy import — written in parallel) for per_event_delta
    and confidence_sequence. Empty input returns an empty frame WITHOUT
    importing evaluate, so a fresh clone renders even before evaluate exists.
    """
    cols = ["n", "ts_forecast", "mean", "lo", "hi"]
    base = headline_deltas(df)
    if base.empty:
        return pd.DataFrame(columns=cols)
    from bot import evaluate  # lazy: contract per the build doc / task spec
    d = evaluate.per_event_delta(base["p"].values, base["q"].values,
                                 base["o"].values)
    mean, lo, hi = evaluate.confidence_sequence(np.asarray(d, dtype=float), rho)
    return pd.DataFrame({"n": np.arange(1, len(base) + 1),
                         "ts_forecast": base["ts_forecast"].values,
                         "mean": np.asarray(mean, dtype=float),
                         "lo": np.asarray(lo, dtype=float),
                         "hi": np.asarray(hi, dtype=float)})


# --- local scoring (small enough to own; keeps panels alive without evaluate)

def brier(p, o) -> float:
    p, o = np.asarray(p, dtype=float), np.asarray(o, dtype=float)
    return float(np.mean((p - o) ** 2))


def log_score(p, o) -> float:
    p = np.clip(np.asarray(p, dtype=float), 0.01, 0.99)
    o = np.asarray(o, dtype=float)
    return float(np.mean(o * np.log(p) + (1 - o) * np.log(1 - p)))


def calibration_table(p, o, bins: int = 10) -> pd.DataFrame:
    """Reliability-diagram bins with binomial standard errors."""
    p, o = np.asarray(p, dtype=float), np.asarray(o, dtype=float)
    if len(p) == 0:
        return pd.DataFrame(columns=["mean_pred", "obs_freq", "n", "se"])
    idx = np.clip(np.digitize(p, np.linspace(0, 1, bins + 1)) - 1, 0, bins - 1)
    rows = []
    for b in range(bins):
        m = idx == b
        n = int(m.sum())
        if not n:
            continue
        obs = float(o[m].mean())
        rows.append({"mean_pred": float(p[m].mean()), "obs_freq": obs, "n": n,
                     "se": float(np.sqrt(max(obs * (1 - obs), 0.0) / n))})
    return pd.DataFrame(rows)


def murphy_decomposition(p, o, bins: int = 10) -> dict:
    """Brier = reliability - resolution + uncertainty (Appendix A)."""
    p, o = np.asarray(p, dtype=float), np.asarray(o, dtype=float)
    if len(p) == 0:
        return {"reliability": None, "resolution": None, "uncertainty": None}
    n_tot, ob = len(o), float(o.mean())
    idx = np.clip(np.digitize(p, np.linspace(0, 1, bins + 1)) - 1, 0, bins - 1)
    rel = res = 0.0
    for b in range(bins):
        m = idx == b
        n = int(m.sum())
        if n:
            rel += n * (float(p[m].mean()) - float(o[m].mean())) ** 2
            res += n * (float(o[m].mean()) - ob) ** 2
    return {"reliability": rel / n_tot, "resolution": res / n_tot,
            "uncertainty": ob * (1 - ob)}


# ---------------------------------------------------------------------------
# Config / DB plumbing
# ---------------------------------------------------------------------------

def load_config() -> dict:
    try:
        import yaml
        with open(ROOT / "config.yaml", "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def resolve_db_path() -> str:
    env = os.environ.get("BOT_DB_PATH")
    if env:
        return env
    cfg_path = (load_config().get("run") or {}).get("db_path")
    if cfg_path:
        p = Path(cfg_path)
        return str(p if p.is_absolute() else ROOT / p)
    return str(DEFAULT_DB)


def _connect(path: str):
    from bot import log as botlog  # creates schema on a fresh clone
    return botlog.connect(path)


@st.cache_data(ttl=60)
def load_all(path: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Cached DB read: (forecasts oldest-first, runs newest-first)."""
    con = _connect(path)
    try:
        return load_forecasts(con), load_runs(con)
    finally:
        con.close()


def get_report(path: str) -> dict:
    """evaluate.report on a fresh connection. Caller wraps in try/except."""
    from bot import evaluate  # lazy: written in parallel
    con = _connect(path)
    try:
        return evaluate.report(con, harness_version=None, include_disputed=False)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Chart builders (altair; one axis each, tooltips on, restrained palette)
# ---------------------------------------------------------------------------

def _cs_chart(cs: pd.DataFrame) -> alt.LayerChart:
    x = alt.X("n:Q", title="resolved forecasts (ts_forecast order)")
    band = alt.Chart(cs).mark_area(color=C_BLUE, opacity=0.18).encode(
        x=x, y=alt.Y("lo:Q", title="Δ_log vs market (nats)"), y2="hi:Q")
    line = alt.Chart(cs).mark_line(color=C_BLUE, strokeWidth=2).encode(
        x=x, y="mean:Q",
        tooltip=[alt.Tooltip("n:Q"), alt.Tooltip("ts_forecast:N"),
                 alt.Tooltip("mean:Q", format=".4f"),
                 alt.Tooltip("lo:Q", format=".4f"),
                 alt.Tooltip("hi:Q", format=".4f")])
    zero = alt.Chart(pd.DataFrame({"y": [0.0]})).mark_rule(
        color=C_MUTED, strokeDash=[4, 4]).encode(y="y:Q")
    return (band + line + zero).properties(height=280)


def _reliability_chart(cal: pd.DataFrame) -> alt.LayerChart:
    diag = alt.Chart(pd.DataFrame({"x": [0.0, 1.0], "y": [0.0, 1.0]})).mark_line(
        color=C_GRID, strokeDash=[4, 4]).encode(x="x:Q", y="y:Q")
    cal = cal.assign(lo=(cal["obs_freq"] - 1.96 * cal["se"]).clip(0, 1),
                     hi=(cal["obs_freq"] + 1.96 * cal["se"]).clip(0, 1))
    bars = alt.Chart(cal).mark_rule(color=C_BLUE, strokeWidth=2).encode(
        x=alt.X("mean_pred:Q", title="mean predicted p",
                scale=alt.Scale(domain=[0, 1])),
        y=alt.Y("lo:Q", title="observed frequency",
                scale=alt.Scale(domain=[0, 1])),
        y2="hi:Q")
    pts = alt.Chart(cal).mark_point(filled=True, size=80, color=C_BLUE).encode(
        x="mean_pred:Q", y="obs_freq:Q",
        tooltip=[alt.Tooltip("mean_pred:Q", format=".3f"),
                 alt.Tooltip("obs_freq:Q", format=".3f"),
                 alt.Tooltip("n:Q"), alt.Tooltip("se:Q", format=".3f")])
    return (diag + bars + pts).properties(height=280)


def _hist(df: pd.DataFrame, col: str, title: str) -> alt.Chart:
    return alt.Chart(df).mark_bar(color=C_BLUE).encode(
        x=alt.X(f"{col}:Q", bin=alt.Bin(maxbins=25), title=title),
        y=alt.Y("count()", title="fills"),
        tooltip=[alt.Tooltip(f"{col}:Q", bin=alt.Bin(maxbins=25)), "count()"],
    ).properties(height=200)


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

def panel_headline(df: pd.DataFrame, path: str, target_n) -> None:
    st.header("1 · Edge over the market — Δ_log with anytime-valid CS")
    base = headline_deltas(df)
    if base.empty:
        st.info("No resolved, non-contaminated forecasts yet — the headline "
                "appears after the first settlements close the loop.")
        return

    overall: dict = {}
    try:
        rep = get_report(path) or {}
        overall = dict(rep.get("overall") or {})
    except Exception as e:  # evaluate absent / raising: show, don't crash
        st.warning(f"evaluate.report unavailable ({type(e).__name__}: {e}) — "
                   "showing the confidence sequence only.")

    cs = pd.DataFrame()
    try:
        from bot import evaluate  # lazy
        rho = float(evaluate.optimal_rho(target_n))
        cs = running_cs(df, rho)
    except Exception as e:
        st.info(f"Confidence sequence unavailable (bot/evaluate.py pending?): "
                f"{type(e).__name__}: {e}")

    line_overall = dict(overall)
    if len(cs):
        last = cs.iloc[-1]
        # The plain-language line quotes the anytime-valid CS band.
        line_overall["ci_lo"], line_overall["ci_hi"] = last["lo"], last["hi"]
        line_overall.setdefault("delta_log", last["mean"])
        if _num(line_overall.get("n")) is None:
            line_overall["n"] = len(cs)

    dl = _num(line_overall.get("delta_log"))
    st.metric("Δ_log vs market baseline (nats, >0 = you beat the price)",
              _fmt(dl))
    st.markdown(f"**{significance_line(line_overall, target_n)}**")
    extras = []
    if _num(overall.get("kappa")) is not None:
        extras.append(f"κ̂ (variance inflation) = {_fmt(overall.get('kappa'), 2)}")
    if _num(overall.get("perm_p")) is not None:
        extras.append(f"paired permutation p = {_fmt(overall.get('perm_p'), 3)}")
    if extras:
        st.caption(" · ".join(extras))
    if len(cs):
        st.altair_chart(_cs_chart(cs), width="stretch")
        st.caption("Band is the anytime-valid confidence sequence — peek "
                   "freely; decide only at N* or if the lower bound crosses "
                   "zero downward.")


def panel_calibration(df: pd.DataFrame) -> None:
    st.header("2 · Calibration & Murphy decomposition")
    base = headline_deltas(df)
    if base.empty:
        st.info("No resolved forecasts yet — calibration needs settled outcomes.")
        return

    left, right = st.columns(2)
    with left:
        st.subheader("Reliability diagram")
        cal = calibration_table(base["p"].values, base["o"].values)
        st.altair_chart(_reliability_chart(cal), width="stretch")
        st.caption("Dashed 45° = perfect calibration; whiskers are ±1.96 "
                   "binomial SE. Expect under-extremeness (points pinched "
                   "toward 0.5), not overconfidence.")
    with right:
        st.subheader("Murphy decomposition (Brier)")
        mur = murphy_decomposition(base["p"].values, base["o"].values)
        mdf = pd.DataFrame({
            "component": ["reliability (want low)", "resolution (want high)",
                          "uncertainty (fixed by world)"],
            "value": [mur["reliability"], mur["resolution"],
                      mur["uncertainty"]]})
        st.altair_chart(
            alt.Chart(mdf).mark_bar(color=C_BLUE).encode(
                y=alt.Y("component:N", sort=None, title=None),
                x=alt.X("value:Q", title="Brier points"),
                tooltip=["component:N", alt.Tooltip("value:Q", format=".4f")],
            ).properties(height=140),
            width="stretch")

    st.subheader("Score table — you vs market vs no-research baseline")
    rows = []
    resolved = df[pd.to_numeric(df["outcome"], errors="coerce").notna()]
    contaminated = pd.to_numeric(resolved.get("contaminated"),
                                 errors="coerce").fillna(0)
    resolved = resolved[contaminated != 1]
    o_all = pd.to_numeric(resolved["outcome"], errors="coerce")
    specs = [("You (p)", "p"),
             ("Market (baseline_market_p)", "baseline_market_p"),
             ("No-research baseline", "baseline_norsrch_p")]
    for label, col in specs:
        vals = pd.to_numeric(resolved.get(col), errors="coerce")
        if label.startswith("Market"):
            vals = vals.fillna(pd.to_numeric(resolved.get("q_mid_snap"),
                                             errors="coerce"))
        m = vals.notna() & o_all.notna()
        if int(m.sum()) == 0:
            rows.append({"forecaster": label, "n": 0, "brier": None,
                         "log score": None})
            continue
        rows.append({"forecaster": label, "n": int(m.sum()),
                     "brier": round(brier(vals[m].values, o_all[m].values), 4),
                     "log score": round(log_score(vals[m].values,
                                                  o_all[m].values), 4)})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def panel_category_version(path: str, has_resolved: bool) -> None:
    st.header("3 · By category (shrunk) & by version")
    if not has_resolved:
        st.info("No resolved forecasts yet — per-category and per-version "
                "splits appear after settlements.")
        return
    try:
        rep = get_report(path) or {}
    except Exception as e:
        st.warning(f"evaluate.report unavailable ({type(e).__name__}: {e}).")
        return

    per_cat = rep.get("per_category") or {}
    if per_cat:
        rows = []
        for name, v in per_cat.items():
            v = v or {}
            rows.append({"category": name, "raw Δ": _num(v.get("delta")),
                         "shrunk Δ": _num(v.get("shrunk")),
                         "se": _num(v.get("se")), "n": v.get("n")})
        cdf = pd.DataFrame(rows)
        long = cdf.melt(id_vars=["category", "se", "n"],
                        value_vars=["raw Δ", "shrunk Δ"],
                        var_name="estimate", value_name="delta").dropna(
                            subset=["delta"])
        if len(long):
            chart = alt.Chart(long).mark_bar().encode(
                y=alt.Y("category:N", sort="-x", title=None),
                x=alt.X("delta:Q", title="Δ_log (nats)"),
                yOffset=alt.YOffset("estimate:N"),
                color=alt.Color("estimate:N",
                                scale=alt.Scale(domain=["raw Δ", "shrunk Δ"],
                                                range=[C_BLUE_LT, C_BLUE]),
                                legend=alt.Legend(title=None, orient="top")),
                tooltip=["category:N", "estimate:N",
                         alt.Tooltip("delta:Q", format=".4f"),
                         alt.Tooltip("se:Q", format=".4f"), "n:Q"],
            ).properties(height=max(120, 44 * cdf["category"].nunique()))
            st.altair_chart(chart, width="stretch")
            st.caption("Raw next to shrunk so the shrinkage is visible — "
                       "allocate/select on the SHRUNK number (winner's-curse "
                       "antidote). Everything shrinking to one value means "
                       "categories aren't yet distinguishable.")
        st.dataframe(cdf, width="stretch", hide_index=True)
    else:
        st.info("No per-category numbers yet.")

    st.subheader("Per harness version")
    per_ver = rep.get("per_version") or {}
    if per_ver:
        vrows = []
        for name, v in per_ver.items():
            row = {"version": name}
            row.update({k: val for k, val in (v or {}).items()})
            vrows.append(row)
        st.dataframe(pd.DataFrame(vrows), width="stretch",
                     hide_index=True)
        st.caption("Compare versions only via concurrent paired A/B on shared "
                   "questions — never by pooling calendar records.")
    else:
        st.info("No per-version numbers yet.")


def panel_funnel(runs: pd.DataFrame) -> None:
    st.header("4 · Selection funnel & cost")
    if runs is None or runs.empty:
        st.info("No pipeline runs recorded yet — the funnel appears after the "
                "first run writes to the runs table.")
        return
    gates = latest_funnel(runs)
    if gates:
        fdf = pd.DataFrame(gates, columns=["gate", "survivors"])
        fdf["order"] = range(len(fdf))
        st.altair_chart(
            alt.Chart(fdf).mark_bar(color=C_BLUE).encode(
                y=alt.Y("gate:N", sort=alt.EncodingSortField("order"),
                        title=None),
                x=alt.X("survivors:Q", title="markets surviving gate"),
                tooltip=["gate:N", "survivors:Q"],
            ).properties(height=max(120, 34 * len(fdf))),
            width="stretch")
    else:
        st.info("No funnel data in any run yet (runs.funnel is empty).")

    st.subheader("Latest runs")
    cols = [c for c in ["id", "ts_start", "ts_end", "status", "stage",
                        "n_ingested", "n_selected", "n_forecast", "n_traded",
                        "cost_usd"] if c in runs.columns]
    st.dataframe(runs[cols].head(15), width="stretch",
                 hide_index=True)


def panel_execution(df: pd.DataFrame) -> None:
    st.header("5 · Execution quality")
    if df is None or df.empty:
        st.info("No forecasts yet — execution stats appear once the decision "
                "layer places (or passes on) orders.")
        return
    traded = df[df["side"].isin(["yes", "no"])]
    if traded.empty:
        st.info("No non-pass decisions yet — everything has been passed on "
                "(which is usually correct).")
        return

    c1, c2, c3 = st.columns(3)
    n_maker = int((traded["order_type"] == "maker").sum())
    n_taker = int((traded["order_type"] == "taker").sum())
    frac = pd.to_numeric(traded["fill_qty_frac"], errors="coerce").fillna(0)
    fill_rate = float((frac > 0).mean())
    c1.metric("Maker orders", n_maker)
    c2.metric("Taker orders", n_taker)
    c3.metric("Fill rate (non-pass, any qty)", f"{fill_rate:.0%}")

    mix = traded["order_type"].fillna("unknown").value_counts().reset_index()
    mix.columns = ["order_type", "count"]
    st.altair_chart(
        alt.Chart(mix).mark_bar().encode(
            y=alt.Y("order_type:N", title=None),
            x=alt.X("count:Q", title="orders"),
            color=alt.Color("order_type:N",
                            scale=alt.Scale(domain=["maker", "taker",
                                                    "unknown"],
                                            range=[C_BLUE, C_ORANGE, C_MUTED]),
                            legend=None),
            tooltip=["order_type:N", "count:Q"],
        ).properties(height=110),
        width="stretch")
    st.caption("Maker-first by design: makers −9.6% vs takers −31.5% average "
               "returns in the Kalshi evidence base.")

    st.subheader("Markouts (toxic-flow detector)")
    any_markout = False
    mcols = st.columns(2)
    for i, (col, title) in enumerate([("markout_5m", "markout at t+5m"),
                                      ("markout_1h", "markout at t+1h")]):
        sub = traded[pd.to_numeric(traded[col], errors="coerce").notna()]
        with mcols[i]:
            if len(sub):
                any_markout = True
                st.altair_chart(_hist(sub, col, f"{title} (prob points)"),
                                width="stretch")
            else:
                st.info(f"No {col} data yet.")
    if any_markout:
        st.caption("Systematically negative markouts mean the flow is "
                   "toxic-selected — widen the toxicity buffer in the "
                   "decision layer.")


def panel_explorer(df: pd.DataFrame) -> None:
    st.header("6 · Per-forecast explorer")
    if df is None or df.empty:
        st.info("No forecasts logged yet — the explorer fills in after the "
                "first pipeline run.")
        return

    view = pd.DataFrame({
        "id": df["id"],
        "ts": df["ts_forecast"],
        "market": df["venue"].astype(str) + ":" + df["market_id"].astype(str),
        "category": df["category"],
        "cluster_id": df["cluster_id"],
        "p": df["p"],
        "q_mid_decide": df["q_mid_decide"],
        "edge": df["edge"],
        "side": df["side"],
        "order_type": df["order_type"],
        "fill_price": df["fill_price"],
        "stake": df["stake"],
        "status": np.where(pd.to_numeric(df["outcome"],
                                         errors="coerce").notna(),
                           "resolved: " + df["outcome"].astype("string"),
                           "open"),
        "harness_version": df["harness_version"],
    }).iloc[::-1]  # newest first for browsing
    st.dataframe(view, width="stretch", hide_index=True, height=350)

    labels = {
        f"#{int(r['id'])} · {r['market']} · p={_fmt(r['p'], 2)} · {r['status']}":
            int(r["id"]) for _, r in view.iterrows()}
    choice = st.selectbox("Drill into a forecast", list(labels))
    row = df[df["id"] == labels[choice]].iloc[0].to_dict()

    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**{row.get('question') or '(no question text)'}**")
        st.markdown("**Rationale**")
        st.write(row.get("rationale") or "—")
        st.markdown("**Resolution-criteria interpretation**")
        st.write(row.get("criteria_interp") or "—")
        note = row.get("resolution_note")
        if note:
            st.markdown("**Resolution note**")
            st.write(note)
    with c2:
        st.markdown("**Sources**")
        try:
            src = json.loads(row.get("sources") or "[]")
        except (ValueError, TypeError):
            src = [row.get("sources")]
        if src:
            for s in src:
                st.write(f"- {s}")
        else:
            st.write("— none logged —")
        st.markdown("**Ensemble**")
        try:
            members = json.loads(row.get("ensemble_members") or "[]")
        except (ValueError, TypeError):
            members = []
        st.write(f"members: {members or '—'}")
        st.write(f"spread (max−min): {_fmt(row.get('ensemble_spread'), 3)}")
        st.markdown("**Flags**")
        if _num(row.get("contaminated")) == 1:
            st.error("contaminated: YES — price-leak scan hit; excluded from "
                     "the headline")
        else:
            st.write("contaminated: no")
        if _num(row.get("retrieval_ok")) == 0:
            st.warning("retrieval_ok: NO — thin/failed retrieval; confidence "
                       "capped")
        else:
            st.write("retrieval_ok: yes")
        if _num(row.get("disputed")) == 1:
            st.warning("disputed resolution")


def panel_positions(df: pd.DataFrame, now: datetime) -> None:
    st.header("7 · Open positions & settlement queue")
    if df is None or df.empty:
        st.info("No forecasts yet — nothing awaiting settlement.")
        return
    is_open = pd.to_numeric(df["outcome"], errors="coerce").isna()
    open_df = df[is_open]
    resolved = df[~is_open]
    disputed_n = int((pd.to_numeric(resolved.get("disputed"), errors="coerce")
                      .fillna(0) == 1).sum())

    c1, c2, c3 = st.columns(3)
    c1.metric("Open forecasts", len(open_df))
    late = set(overdue_ids(open_df, now))
    c2.metric("Overdue (> 3 days past resolve_by)", len(late))
    c3.metric("Disputed among resolved", disputed_n)
    if disputed_n:
        st.caption("Disputed rows are excluded from the headline "
                   "(include_disputed=False) — resolution risk is measured, "
                   "not forecasting skill.")

    if open_df.empty:
        st.info("Settlement queue is empty — no open positions.")
        return
    q = pd.DataFrame({
        "id": open_df["id"],
        "ts": open_df["ts_forecast"],
        "market": open_df["venue"].astype(str) + ":" +
                  open_df["market_id"].astype(str),
        "side": open_df["side"],
        "stake": open_df["stake"],
        "resolve_by": open_df["resolve_by"],
        "overdue": open_df["id"].isin(late),
    }).sort_values("resolve_by", na_position="last")
    st.dataframe(q, width="stretch", hide_index=True)
    if late:
        st.error(f"{len(late)} position(s) overdue for settlement — run the "
                 "settlement tracker (never running it is a named pitfall).")


def panel_ops(df: pd.DataFrame, runs: pd.DataFrame, now: datetime) -> None:
    st.header("8 · Ops & health")
    if (runs is None or runs.empty) and (df is None or df.empty):
        st.info("Fresh database — no runs or forecasts yet. Kick off the "
                "pipeline to populate the log.")
        return

    # Heartbeat: latest successful run must be < HEARTBEAT_MAX_HOURS old.
    last_ok_ts = None
    if runs is not None and not runs.empty:
        ok = runs[runs["status"] == "ok"]
        if len(ok):
            r0 = ok.iloc[0]
            last_ok_ts = r0["ts_end"] or r0["ts_start"]
    if last_ok_ts:
        try:
            age_h = (now - _parse_ts(last_ok_ts)).total_seconds() / 3600
            if age_h > HEARTBEAT_MAX_HOURS:
                st.error(f"Heartbeat: last successful run was {age_h:.0f}h ago "
                         f"(> {HEARTBEAT_MAX_HOURS}h) — the pipeline may be "
                         "silently down.")
            else:
                st.success(f"Heartbeat OK — last successful run {age_h:.1f}h "
                           "ago.")
        except (ValueError, TypeError):
            st.warning("Heartbeat: could not parse the last ok-run timestamp.")
    else:
        st.warning("No successful run recorded yet.")

    c1, c2, c3, c4 = st.columns(4)
    if runs is not None and not runs.empty:
        last = runs.iloc[0]
        c1.metric("Last run status", str(last["status"]))
        run_cost = pd.to_numeric(runs["cost_usd"], errors="coerce").fillna(0)
        c2.metric("Cumulative run cost", f"${float(run_cost.sum()):,.2f}")
    else:
        c1.metric("Last run status", "—")
        c2.metric("Cumulative run cost", "$0.00")
    n_total = 0 if df is None else len(df)
    n_resolved = 0 if df is None or df.empty else int(
        pd.to_numeric(df["outcome"], errors="coerce").notna().sum())
    c3.metric("Rows (total / open / resolved)",
              f"{n_total} / {n_total - n_resolved} / {n_resolved}")
    # Deliberately a single small metric: P&L is one noisy realized path.
    c4.metric("Paper P&L (de-emphasized by design)",
              f"{paper_pnl(df):+.2f}" if n_total else "—")
    c4.caption("Δ_log above is the headline; this is not.")

    if runs is not None and not runs.empty:
        errs = runs[runs["status"] == "error"]
        if len(errs):
            st.subheader("Failed runs")
            st.dataframe(errs[["id", "ts_start", "stage", "error"]].head(10),
                         width="stretch", hide_index=True)
        else:
            st.caption("No failed runs on record.")


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Forecast bot — observability",
                       layout="wide")
    st.title("Prediction-market bot — observability")
    st.caption("Δ_log with honest uncertainty is the headline; paper P&L is "
               "deliberately small (doc §4). Contaminated rows are excluded "
               "from the headline; disputed rows from the report.")

    path = resolve_db_path()
    cfg = load_config()
    target_n = cfg.get("target_n_clusters", DEFAULT_TARGET_N)
    now = datetime.now(timezone.utc)

    with st.sidebar:
        st.markdown(f"**DB:** `{path}`")
        st.markdown(f"**Pre-registered N\\*:** {target_n} clusters · "
                    f"Δ\\* = {cfg.get('min_edge_nats', '—')} nats")
        st.markdown(f"**Versions:** harness `{cfg.get('harness_version', '?')}`"
                    f" · decision `{cfg.get('decision_version', '?')}`")
        if st.button("Refresh data (clear 60s cache)"):
            load_all.clear()

    try:
        df, runs = load_all(path)
    except Exception as e:
        st.error(f"Could not open the forecast log at {path}: "
                 f"{type(e).__name__}: {e}")
        return

    if df.empty and runs.empty:
        st.info("Fresh database — no forecasts or runs logged yet. Every "
                "panel below will populate as the pipeline writes to "
                f"`{path}`.")

    has_resolved = (not df.empty and
                    pd.to_numeric(df["outcome"], errors="coerce").notna().any())

    panel_headline(df, path, target_n)
    st.divider()
    panel_calibration(df)
    st.divider()
    panel_category_version(path, has_resolved)
    st.divider()
    panel_funnel(runs)
    st.divider()
    panel_execution(df)
    st.divider()
    panel_explorer(df)
    st.divider()
    panel_positions(df, now)
    st.divider()
    panel_ops(df, runs, now)


# Standard streamlit single-file pattern: run under `streamlit run` (runtime
# exists) or direct `python dashboard/app.py`; plain `import dashboard.app`
# stays side-effect-free.
if __name__ == "__main__" or st.runtime.exists():
    main()
