"""Evaluation & reconciliation engine (§2.8) — resolved rows -> "is there an
edge, and where?", with uncertainty that survives correlated events and
continuous peeking.

Math per Appendix A, core scoring per Appendix B, statistics notes per D.5.
numpy + stdlib only. Reads the append-only log via bot.log; never writes it.

Conventions: p = our probability, q = the market's, o in {0,1} the outcome,
d_i = per-event log-score difference (mean(d) == delta_log), clusters =
underlying real-world events (the evaluation unit — resample/permute these,
never rows).
"""
from __future__ import annotations

import sqlite3
from statistics import NormalDist

import numpy as np

from bot import log as botlog

CLIP = 0.01                              # log-score clamp bounds [0.01, 0.99]


# ---------------------------------------------------------------- core scoring

def brier(p, o):
    """Mean squared error on probabilities; 0.25 = always saying 0.5."""
    return float(np.mean((np.asarray(p, float) - np.asarray(o, float)) ** 2))


def log_score(p, o):
    """Mean log score, p clipped to [0.01, 0.99]; <=0, closer to 0 is better."""
    p = np.clip(np.asarray(p, float), CLIP, 1 - CLIP)
    o = np.asarray(o, float)
    return float(np.mean(o * np.log(p) + (1 - o) * np.log(1 - p)))


def delta_log(p, q, o):
    """Headline: log-score margin over the market. >0 => we beat the price."""
    return log_score(p, o) - log_score(q, o)


def per_event_delta(p, q, o) -> np.ndarray:
    """d_i = o·ln(p/q) + (1−o)·ln((1−p)/(1−q)), same clip as log_score.
    mean(d) == delta_log(p, q, o)."""
    p = np.clip(np.asarray(p, float), CLIP, 1 - CLIP)
    q = np.clip(np.asarray(q, float), CLIP, 1 - CLIP)
    o = np.asarray(o, float)
    return o * np.log(p / q) + (1 - o) * np.log((1 - p) / (1 - q))


def calibration(p, o, bins=10):
    """Reliability-diagram points: [(mean_pred, obs_freq, n, se)] per non-empty
    bin, binomial SE = sqrt(obar*(1-obar)/n)."""
    p, o = np.asarray(p, float), np.asarray(o, float)
    idx = np.clip(np.digitize(p, np.linspace(0, 1, bins + 1)) - 1, 0, bins - 1)
    out = []
    for b in range(bins):
        m = idx == b
        n = int(m.sum())
        if n:
            ob = float(o[m].mean())
            out.append((float(p[m].mean()), ob, n, float(np.sqrt(ob * (1 - ob) / n))))
    return out


def murphy(p, o, bins=10):
    """Murphy decomposition: BS = reliability - resolution + uncertainty."""
    p, o = np.asarray(p, float), np.asarray(o, float)
    N = len(o)
    ob = o.mean()
    idx = np.clip(np.digitize(p, np.linspace(0, 1, bins + 1)) - 1, 0, bins - 1)
    rel = res = 0.0
    for b in range(bins):
        m = idx == b
        n = m.sum()
        if n:
            rel += n * (p[m].mean() - o[m].mean()) ** 2
            res += n * (o[m].mean() - ob) ** 2
    return dict(reliability=rel / N, resolution=res / N, uncertainty=ob * (1 - ob))


# ------------------------------------------------------ cluster-aware inference

def _members(clusters):
    clusters = np.asarray(clusters)
    uniq = np.unique(clusters)
    return uniq, {c: np.flatnonzero(clusters == c) for c in uniq}


def _boot_deltas(p, q, o, clusters, n, rng) -> np.ndarray:
    """Bootstrap distribution of delta_log resampling CLUSTERS, members kept
    together (Appendix B reference algorithm)."""
    uniq, members = _members(clusters)
    k = len(uniq)
    if k < 2:
        raise ValueError("cluster bootstrap requires >=2 clusters")
    s = np.empty(n)
    for j in range(n):
        i = np.concatenate([members[c] for c in uniq[rng.integers(0, k, k)]])
        s[j] = delta_log(p[i], q[i], o[i])
    return s


def cluster_bootstrap_ci(p, q, o, clusters, n=10000, a=0.05, seed=None):
    """CI on delta_log resampling CLUSTERS (underlying events), not rows."""
    p, q, o = (np.asarray(x, float) for x in (p, q, o))
    s = _boot_deltas(p, q, o, clusters, n, np.random.default_rng(seed))
    return tuple(float(x) for x in np.quantile(s, [a / 2, 1 - a / 2]))


def paired_permutation_p(p, q, o, clusters, n=10000, seed=None):
    """Two-sided p-value: swap p<->q per CLUSTER under the exchangeable null."""
    p, q, o = (np.asarray(x, float) for x in (p, q, o))
    clusters = np.asarray(clusters)
    rng = np.random.default_rng(seed)
    obs = delta_log(p, q, o)
    uniq = np.unique(clusters)
    hits = 0
    for _ in range(n):
        swap = np.isin(clusters, uniq[rng.random(len(uniq)) < 0.5])
        pp, qq = np.where(swap, q, p), np.where(swap, p, q)
        hits += abs(delta_log(pp, qq, o)) >= abs(obs)
    return hits / n


def n_eff(clusters, d):
    """Effective sample size N/(1+(m̄−1)ρ̂); ρ̂ = one-way-ANOVA intra-cluster
    correlation of d, clipped to [0,1]. All singletons => N."""
    clusters = np.asarray(clusters)
    d = np.asarray(d, float)
    N = len(d)
    uniq, inv, counts = np.unique(clusters, return_inverse=True, return_counts=True)
    k = len(uniq)
    if k == N:                            # all singletons: no clustering penalty
        return float(N)
    if k < 2:                             # one cluster = one draw (rho -> 1)
        return 1.0
    means = np.array([d[inv == i].mean() for i in range(k)])
    msb = float(np.sum(counts * (means - d.mean()) ** 2)) / (k - 1)
    msw = float(np.sum((d - means[inv]) ** 2)) / (N - k)
    m0 = (N - np.sum(counts ** 2) / N) / (k - 1)   # ANOVA-adjusted cluster size
    denom = msb + (m0 - 1) * msw
    rho = float(np.clip((msb - msw) / denom, 0.0, 1.0)) if denom > 0 else 0.0
    return float(N / (1 + (N / k - 1) * rho))


def kappa(d, dlog):
    """Variance-inflation ratio κ̂ = Var(d)/(2·Δ_log); ~1 = calibrated
    best case, 2.5–8 realistic (D.5)."""
    d = np.asarray(d, float)
    return float(np.var(d, ddof=1) / (2 * max(abs(dlog), 1e-9)))


# ---------------------------------------------------------- sequential analysis

def _cs_radius_factor(t, rho, alpha):
    """Waudby-Smith–Ramdas asymptotic CS radius / sd_t (Appendix A)."""
    return np.sqrt(2 * (t * rho ** 2 + 1) / (t ** 2 * rho ** 2)
                   * np.log(np.sqrt(t * rho ** 2 + 1) / alpha))


def confidence_sequence(d, rho, alpha=0.05):
    """Anytime-valid CS on the running mean of per-event deltas d.
    Returns (mean, lo, hi) arrays over t = 1..len(d) — peek freely."""
    d = np.asarray(d, float)
    t = np.arange(1, len(d) + 1)
    mean = np.cumsum(d) / t
    sd = np.sqrt(np.maximum(np.cumsum(d ** 2) / t - mean ** 2, 1e-12))
    rad = sd * _cs_radius_factor(t, rho, alpha)
    return mean, mean - rad, mean + rad


def optimal_rho(t_star, alpha=0.05):
    """rho minimizing the CS radius at the planning horizon t* (pre-registered
    N). Coarse log-grid then linear refinement; numpy only."""
    t = float(t_star)
    def rad(rho): return _cs_radius_factor(t, rho, alpha)
    coarse = np.logspace(-4, 1, 500)
    r0 = coarse[np.argmin(rad(coarse))]
    fine = np.linspace(r0 / 2, r0 * 2, 2001)
    return float(fine[np.argmin(rad(fine))])


# ----------------------------------------------------------------------- power

def required_n(delta, power=0.8, alpha=0.05, kappa=1.0):
    """Clusters needed for a two-sided test of Δ_log = delta, using the
    Var(d) = 2·delta·kappa identity: N = (z_{α/2}+z_β)² · 2κ/Δ (Appendix A)."""
    z = NormalDist().inv_cdf
    return (z(1 - alpha / 2) + z(power)) ** 2 * 2 * kappa / delta


# ---------------------------------------------------------- category selection

def eb_shrink(deltas, ses):
    """Empirical-Bayes shrinkage of per-category deltas (winner's-curse
    antidote); allocate/select on the result."""
    deltas, ses = np.asarray(deltas, float), np.asarray(ses, float)
    if len(deltas) < 2:
        return deltas.copy()
    w = 1 / ses ** 2
    mu = np.sum(w * deltas) / np.sum(w)
    tau2 = max(0.0, np.var(deltas, ddof=1) - np.mean(ses ** 2))
    return mu + tau2 / (tau2 + ses ** 2) * (deltas - mu)


# ----------------------------------------------------------------- fast signal

def clv(logit_q_trade, logit_q_close, direction):
    """CLV-style signal: mean logit movement of the market from trade time
    toward our side (+1 = we were on yes, -1 = on no). Diagnostic only —
    never the headline (D.5)."""
    move = np.asarray(logit_q_close, float) - np.asarray(logit_q_trade, float)
    return float(np.mean(move * np.asarray(direction, float)))


# ---------------------------------------------------------------------- report

_OVERALL_KEYS = ("n", "n_clusters", "n_eff", "delta_log", "ci_lo", "ci_hi",
                 "perm_p", "kappa", "brier_p", "brier_q", "log_p", "log_q",
                 "murphy")


def _segment(name_of, usable, p, q, o, clusters, n_boot, seed):
    """Per-segment delta/se/n; se via cluster bootstrap (needs >=2 clusters)."""
    out = {}
    names = np.asarray([name_of(r) for r in usable])
    for i, name in enumerate(np.unique(names)):
        m = names == name
        d = per_event_delta(p[m], q[m], o[m])
        seg = dict(delta=float(d.mean()), se=None, n=int(m.sum()))
        if len(np.unique(clusters[m])) >= 2:
            s = _boot_deltas(p[m], q[m], o[m], clusters[m], n_boot,
                             np.random.default_rng(None if seed is None else seed + i))
            seg["se"] = float(s.std(ddof=1))
        out[str(name)] = seg
    return out


def report(con: sqlite3.Connection, harness_version: str | None = None,
           include_disputed: bool = False, n_boot: int = 10000,
           n_perm: int = 10000, bins: int = 10, seed: int | None = None) -> dict:
    """Full evaluation over resolved rows in the append-only log.

    Headline excludes contaminated rows (counted) and, unless include_disputed,
    disputed rows (counted). Market comparison is p vs baseline_market_p
    (q at forecast time; falls back to q_mid_snap when null). Tiny N degrades
    to None fields, never crashes; bootstrap/permutation need >=2 clusters.
    """
    every = botlog.resolved_rows(con, harness_version, include_disputed=True)
    rows = every if include_disputed else [r for r in every if not r["disputed"]]
    counts = dict(contaminated=sum(1 for r in rows if r["contaminated"]),
                  disputed_excluded=len(every) - len(rows))
    clean = [r for r in rows if not r["contaminated"]]

    def q_of(r):
        return r["baseline_market_p"] if r["baseline_market_p"] is not None \
            else r["q_mid_snap"]

    usable = [r for r in clean if q_of(r) is not None]
    out: dict = {"overall": dict.fromkeys(_OVERALL_KEYS),
                 "per_category": {}, "per_version": {}, "calibration": [],
                 "baseline": {"delta_log_vs_norsrch": None, "n": 0},
                 "counts": counts}
    out["overall"]["n"] = len(usable)
    if not usable:
        return out

    p = np.array([r["p"] for r in usable], float)
    q = np.array([q_of(r) for r in usable], float)
    o = np.array([r["outcome"] for r in usable], float)
    clusters = np.asarray([r["cluster_id"] for r in usable])
    d = per_event_delta(p, q, o)
    dl = float(d.mean())
    k = len(np.unique(clusters))

    ov = out["overall"]
    ov.update(n_clusters=k, n_eff=n_eff(clusters, d), delta_log=dl,
              kappa=kappa(d, dl) if len(d) >= 2 else None,
              brier_p=brier(p, o), brier_q=brier(q, o),
              log_p=log_score(p, o), log_q=log_score(q, o),
              murphy=murphy(p, o, bins))
    if k >= 2:
        ov["ci_lo"], ov["ci_hi"] = cluster_bootstrap_ci(
            p, q, o, clusters, n=n_boot, seed=seed)
        ov["perm_p"] = paired_permutation_p(
            p, q, o, clusters, n=n_perm,
            seed=None if seed is None else seed + 1)

    # per-category (with EB shrinkage across categories) and per-version
    out["per_category"] = _segment(lambda r: r["category"] or "", usable,
                                   p, q, o, clusters, n_boot,
                                   None if seed is None else seed + 100)
    out["per_version"] = _segment(lambda r: r["harness_version"], usable,
                                  p, q, o, clusters, n_boot,
                                  None if seed is None else seed + 200)
    shrinkable = [(name, seg) for name, seg in out["per_category"].items()
                  if seg["se"] is not None]
    if len(shrinkable) >= 2:
        shrunk = eb_shrink([seg["delta"] for _, seg in shrinkable],
                           [max(seg["se"], 1e-9) for _, seg in shrinkable])
        for (name, _), s in zip(shrinkable, shrunk):
            out["per_category"][name]["shrunk"] = float(s)
    for seg in out["per_category"].values():
        seg.setdefault("shrunk", seg["delta"])   # < 2 shrinkable: grand-mean-free

    out["calibration"] = calibration(p, o, bins)

    # no-research baseline: harness p vs one-shot no-research p, where present
    nr = [(r["p"], r["baseline_norsrch_p"], r["outcome"]) for r in usable
          if r["baseline_norsrch_p"] is not None]
    if nr:
        pn, bn, on = map(np.array, zip(*nr))
        out["baseline"] = {"delta_log_vs_norsrch": float(delta_log(pn, bn, on)),
                           "n": len(nr)}
    return out
