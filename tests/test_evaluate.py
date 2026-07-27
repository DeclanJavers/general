"""Tests for the evaluation engine (bot/evaluate.py).

These verify the math, not just the plumbing: hand-computed scores, the
Murphy identity, the Var(d) = 2*delta identity by simulation, cluster-aware
inference against exact duplicates, anytime validity of the confidence
sequence, the power formula, EB shrinkage limits, and report() end-to-end
on a temporary log DB. All randomness is seeded.
"""
from __future__ import annotations

import numpy as np
import pytest

from bot import evaluate as ev
from bot import log as botlog


def logit(x):
    return np.log(np.asarray(x, float) / (1 - np.asarray(x, float)))


def sigmoid(z):
    return 1 / (1 + np.exp(-np.asarray(z, float)))


# ---------------------------------------------------------------- core scoring

def test_brier_hand():
    p, o = [0.8, 0.3, 0.6], [1, 0, 1]
    assert ev.brier(p, o) == pytest.approx((0.04 + 0.09 + 0.16) / 3, abs=1e-12)


def test_log_score_hand_and_clip():
    p, o = [0.8, 0.3, 0.6], [1, 0, 1]
    expect = (np.log(0.8) + np.log(0.7) + np.log(0.6)) / 3
    assert ev.log_score(p, o) == pytest.approx(expect, abs=1e-12)
    # clip to [0.01, 0.99]: extreme inputs score as 0.99
    assert ev.log_score([0.999, 0.001], [1, 0]) == pytest.approx(np.log(0.99),
                                                                 abs=1e-12)


def test_delta_log_and_per_event_hand():
    p, q, o = [0.8, 0.3, 0.6], [0.6, 0.5, 0.5], [1, 0, 1]
    expect_d = np.array([np.log(0.8 / 0.6), np.log(0.7 / 0.5),
                         np.log(0.6 / 0.5)])
    d = ev.per_event_delta(p, q, o)
    assert np.allclose(d, expect_d, atol=1e-12)
    assert ev.delta_log(p, q, o) == pytest.approx(expect_d.mean(), abs=1e-12)
    assert d.mean() == pytest.approx(ev.delta_log(p, q, o), abs=1e-12)


def test_calibration_bins_and_se():
    p = [0.05, 0.05, 0.05, 0.05, 0.85, 0.85]
    o = [0, 0, 0, 1, 1, 1]
    bins = ev.calibration(p, o, bins=10)
    assert len(bins) == 2
    mp, ob, n, se = bins[0]
    assert (mp, ob, n) == (pytest.approx(0.05), pytest.approx(0.25), 4)
    assert se == pytest.approx(np.sqrt(0.25 * 0.75 / 4), abs=1e-12)
    mp, ob, n, se = bins[1]
    assert (ob, n, se) == (1.0, 2, 0.0)


def test_murphy_identity():
    # exact BS = rel - res + unc requires within-bin-constant forecasts:
    # draw p from the 10 bin centers, o ~ Bernoulli(p).
    rng = np.random.default_rng(0)
    p = rng.choice(np.arange(0.05, 1.0, 0.1), size=5000)
    o = (rng.random(5000) < p).astype(float)
    m = ev.murphy(p, o, bins=10)
    assert (m["reliability"] - m["resolution"] + m["uncertainty"]
            == pytest.approx(ev.brier(p, o), abs=1e-9))
    assert m["reliability"] >= 0 and m["resolution"] >= 0


# ------------------------------------------------- Var(d) = 2*delta (kappa)

def test_kappa_identity_simulation():
    rng = np.random.default_rng(1)
    n = 200_000
    r = rng.beta(2, 2, n)
    o = (rng.random(n) < r).astype(float)
    q = sigmoid(logit(r) + rng.normal(0, 0.3, n))
    # perfect forecaster p = r: all disagreement is signal -> kappa ~ 1
    d = ev.per_event_delta(r, q, o)
    k = ev.kappa(d, float(d.mean()))
    assert 0.85 <= k <= 1.3
    # independent logit noise on p too: noise inflates Var(d) faster than delta
    p = sigmoid(logit(r) + rng.normal(0, 0.25, n))
    q2 = sigmoid(logit(r) + rng.normal(0, 0.45, n))
    d2 = ev.per_event_delta(p, q2, o)
    assert float(d2.mean()) > 0
    assert ev.kappa(d2, float(d2.mean())) > 1.5


# ------------------------------------------------------ cluster-aware inference

def _sim(k, rng, sd_q=0.5):
    r = rng.beta(2, 2, k)
    o = (rng.random(k) < r).astype(float)
    q = sigmoid(logit(r) + rng.normal(0, sd_q, k))
    return r, q, o  # p = r (perfect)


def test_cluster_bootstrap_duplicates_vs_iid():
    rng = np.random.default_rng(2)
    p, q, o = _sim(50, rng)
    cl = np.arange(50)
    lo, hi = ev.cluster_bootstrap_ci(p, q, o, cl, n=4000, seed=3)
    w_dedup = hi - lo
    # 50 clusters x 4 exact duplicates: adds zero information
    P, Q, O, CL = (np.repeat(x, 4) for x in (p, q, o, cl))
    lo2, hi2 = ev.cluster_bootstrap_ci(P, Q, O, CL, n=4000, seed=4)
    w_dup = hi2 - lo2
    assert abs(w_dup / w_dedup - 1) < 0.25
    # naive iid bootstrap over the 200 rows understates the CI
    rng2 = np.random.default_rng(5)
    s = np.empty(4000)
    for j in range(4000):
        i = rng2.integers(0, 200, 200)
        s[j] = ev.delta_log(P[i], Q[i], O[i])
    w_iid = np.quantile(s, 0.975) - np.quantile(s, 0.025)
    assert w_dup > 1.5 * w_iid


def test_cluster_bootstrap_needs_two_clusters():
    with pytest.raises(ValueError):
        ev.cluster_bootstrap_ci([0.6, 0.7], [0.5, 0.5], [1, 1], ["a", "a"],
                                n=10)


def test_permutation_null_and_edge():
    rng = np.random.default_rng(6)
    k = 150
    r = rng.beta(2, 2, k)
    o = (rng.random(k) < r).astype(float)
    # symmetric null: p and q equally noisy around truth
    p = sigmoid(logit(r) + rng.normal(0, 0.4, k))
    q = sigmoid(logit(r) + rng.normal(0, 0.4, k))
    assert ev.paired_permutation_p(p, q, o, np.arange(k), n=1000, seed=7) > 0.05
    # strong true edge: perfect p vs very noisy q on 400 events
    k2 = 400
    r2 = rng.beta(2, 2, k2)
    o2 = (rng.random(k2) < r2).astype(float)
    q2 = sigmoid(logit(r2) + rng.normal(0, 0.9, k2))
    assert ev.paired_permutation_p(r2, q2, o2, np.arange(k2),
                                   n=1000, seed=8) < 0.01


def test_n_eff_singletons_and_duplicates():
    rng = np.random.default_rng(9)
    d = rng.normal(0, 1, 60)
    assert ev.n_eff(np.arange(60), d) == 60.0
    # 25 clusters of 4 exact duplicates: rho = 1 -> N_eff = N/4
    d4 = np.repeat(rng.normal(0, 1, 25), 4)
    ne = ev.n_eff(np.repeat(np.arange(25), 4), d4)
    assert abs(ne - 25) / 25 < 0.2


def test_kappa_function():
    d = np.array([0.1, -0.1, 0.3, 0.05])
    assert ev.kappa(d, 0.05) == pytest.approx(np.var(d, ddof=1) / 0.1)
    assert ev.kappa(d, 0.0) > 0  # guarded denominator, no crash


# ---------------------------------------------------------- sequential analysis

def test_confidence_sequence_anytime_validity():
    rng = np.random.default_rng(10)
    T, sims = 300, 200
    rho = ev.optimal_rho(T)
    ok = 0
    for _ in range(sims):
        d = rng.normal(0, 1, T)  # null: true mean 0
        _, lo, hi = ev.confidence_sequence(d, rho)
        # burn-in t >= 10: the plug-in sd needs a few observations
        if np.all((lo[9:] <= 0) & (0 <= hi[9:])):
            ok += 1
    assert ok / sims >= 0.90  # anytime 95% coverage, with asymptotic slack


def test_confidence_sequence_radius_decreasing():
    rho = ev.optimal_rho(300)
    d = np.tile([1.0, -1.0], 500)  # at even t: mean 0, sd exactly 1
    mean, lo, hi = ev.confidence_sequence(d, rho)
    rad_even = (hi - mean)[1::2]
    assert np.all(np.diff(rad_even) < 0)


def test_optimal_rho_minimizes_radius_at_horizon():
    t_star = 315
    rho = ev.optimal_rho(t_star)
    d = np.tile([1.0, -1.0], t_star)  # sd = 1 at even t
    def radius_at_horizon(r):
        _, lo, hi = ev.confidence_sequence(d, r)
        return (hi - lo)[2 * (t_star // 2) - 1]  # even index ~ t_star
    r_opt = radius_at_horizon(rho)
    assert r_opt <= radius_at_horizon(rho * 2) + 1e-12
    assert r_opt <= radius_at_horizon(rho / 2) + 1e-12


# ----------------------------------------------------------------------- power

def test_required_n():
    n = ev.required_n(0.05)
    assert abs(n - 315) / 315 < 0.10  # (1.96 + 0.8416)^2 * 2 / 0.05 ~ 314
    assert ev.required_n(0.05, kappa=3.0) == pytest.approx(3 * n)
    assert ev.required_n(0.10) == pytest.approx(n / 2)


# ---------------------------------------------------------- category selection

def test_eb_shrink_limits():
    deltas = np.array([0.10, -0.05, 0.30])
    # huge SEs: tau2 -> 0, everything shrinks to the (precision-weighted) mean
    big = ev.eb_shrink(deltas, [10.0, 10.0, 10.0])
    assert np.allclose(big, deltas.mean(), atol=1e-6)
    assert np.ptp(big) < 1e-6
    # tiny SEs: shrinkage factor -> 1, estimates ~ unshrunk
    small = ev.eb_shrink(deltas, [1e-5, 1e-5, 1e-5])
    assert np.allclose(small, deltas, atol=1e-6)


# ----------------------------------------------------------------- fast signal

def test_clv_hand():
    # yes-side (+1): market moving up in logit is good; no-side (-1) mirrored
    assert ev.clv([0.0, 0.0], [1.0, -1.0], [1, -1]) == pytest.approx(1.0)
    assert ev.clv([0.5], [0.1], [1]) == pytest.approx(-0.4)


# ---------------------------------------------------------------------- report

def _insert(con, rng, cluster, j, cat, r, outcome, contaminated=0):
    # market is under-extreme (hugs 0.5); our p is near-truth => a known,
    # structural positive edge: every per-event delta is > 0 (jitter cannot
    # flip the sign), so headline positivity is by construction, not luck
    q = float(sigmoid(0.35 * logit(r) + rng.normal(0, 0.15)))
    p = float(np.clip(sigmoid(logit(r) + rng.normal(0, 0.05)), 0.03, 0.97))
    nr = float(sigmoid(logit(r) + rng.normal(0, 1.1)))
    rid = botlog.insert_forecast(
        con, venue="kalshi", market_id=f"M{cluster}-{j}", question="q?",
        category=cat, cluster_id=f"ev{cluster}", p=p, q_mid_snap=q,
        # exercise the fallback: half the rows carry q only in q_mid_snap
        baseline_market_p=q if j == 0 else None, baseline_norsrch_p=nr,
        side="yes", harness_version="h1", decision_version="d1",
        contaminated=contaminated)
    return rid, outcome


def test_report_end_to_end(tmp_path):
    rng = np.random.default_rng(12)
    con = botlog.connect(tmp_path / "t.db")
    cats = ["legislative", "regulatory", "corporate"]
    settle_queue = []
    for c in range(20):  # 20 clusters x 2 rows = 40 headline rows
        outcome = c % 2                  # balanced outcomes
        r = 0.78 if outcome else 0.22    # true probability, matches outcome
        for j in range(2):
            settle_queue.append(_insert(con, rng, c, j, cats[c % 3], r, outcome))
    # contaminated rows (absurdly wrong): counted, excluded from headline
    for j in range(2):
        rid = botlog.insert_forecast(
            con, venue="kalshi", market_id=f"C{j}", category="legislative",
            cluster_id=f"cont{j}", p=0.97, q_mid_snap=0.5, baseline_market_p=0.5,
            side="yes", harness_version="h1", decision_version="d1",
            contaminated=1)
        settle_queue.append((rid, 0))
    # disputed row: excluded (and counted) unless include_disputed
    rid = botlog.insert_forecast(
        con, venue="kalshi", market_id="D0", category="regulatory",
        cluster_id="disp0", p=0.9, q_mid_snap=0.5, baseline_market_p=0.5,
        side="yes", harness_version="h1", decision_version="d1", contaminated=0)
    botlog.settle(con, rid, 1, disputed=True)
    for rid, outcome in settle_queue:
        botlog.settle(con, rid, outcome)

    rep = ev.report(con, n_boot=500, n_perm=500, seed=42)
    ov = rep["overall"]
    assert ov["n"] == 40 and ov["n_clusters"] == 20
    assert 20 <= ov["n_eff"] <= 40
    assert ov["delta_log"] > 0.1  # known structural edge
    assert ov["ci_lo"] < ov["delta_log"] < ov["ci_hi"]
    assert ov["ci_lo"] > 0  # every per-event delta is positive by construction
    assert ov["perm_p"] < 0.05
    assert ov["kappa"] > 0
    assert ov["brier_p"] < ov["brier_q"] and ov["log_p"] > ov["log_q"]
    assert set(ov["murphy"]) == {"reliability", "resolution", "uncertainty"}
    assert set(rep["per_category"]) == set(cats)
    for seg in rep["per_category"].values():
        assert {"delta", "se", "n", "shrunk"} <= set(seg)
        assert seg["se"] is not None and seg["n"] > 0
    # shrinkage pulls toward the grand mean, never past the raw estimate
    raw = [s["delta"] for s in rep["per_category"].values()]
    for seg in rep["per_category"].values():
        lo_b = min(seg["delta"], np.mean(raw)) - 0.05
        hi_b = max(seg["delta"], np.mean(raw)) + 0.05
        assert lo_b <= seg["shrunk"] <= hi_b
    assert set(rep["per_version"]) == {"h1"}
    assert rep["per_version"]["h1"]["n"] == 40
    assert rep["calibration"] and all(len(b) == 4 for b in rep["calibration"])
    assert rep["baseline"]["n"] == 40
    assert rep["baseline"]["delta_log_vs_norsrch"] is not None
    assert rep["counts"] == {"contaminated": 2, "disputed_excluded": 1}
    # contaminated rows would have tanked the headline if included
    assert ev.delta_log([0.97], [0.5], [0]) < -1.0

    rep_d = ev.report(con, include_disputed=True, n_boot=200, n_perm=200,
                      seed=43)
    assert rep_d["overall"]["n"] == 41
    assert rep_d["counts"]["disputed_excluded"] == 0


def test_report_tiny_n(tmp_path):
    con = botlog.connect(tmp_path / "e.db")
    rep = ev.report(con)  # empty DB: None fields, no crash
    assert rep["overall"]["n"] == 0
    assert rep["overall"]["delta_log"] is None
    assert rep["counts"] == {"contaminated": 0, "disputed_excluded": 0}
    # one resolved row: stats where possible, no CI/permutation (needs >=2)
    rid = botlog.insert_forecast(
        con, venue="kalshi", market_id="X", cluster_id="c1", p=0.7,
        q_mid_snap=0.5, side="yes", harness_version="h1",
        decision_version="d1")
    botlog.settle(con, rid, 1)
    rep = ev.report(con)
    ov = rep["overall"]
    assert ov["n"] == 1 and ov["n_clusters"] == 1 and ov["n_eff"] == 1.0
    assert ov["delta_log"] == pytest.approx(np.log(0.7 / 0.5))
    assert ov["ci_lo"] is None and ov["ci_hi"] is None and ov["perm_p"] is None
    assert ov["kappa"] is None  # variance undefined at n=1
    assert rep["per_category"][""]["shrunk"] == rep["per_category"][""]["delta"]
