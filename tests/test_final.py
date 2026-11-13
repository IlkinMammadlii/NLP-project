"""Known-answer tests for the final queue's selection rule (run_final.py) and the pre-registered matched
comparison (analysis/matched.py). CPU, seconds. Run: python tests/test_final.py"""
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "analysis"))

import matched as M  # noqa: E402
import run_final as R  # noqa: E402

IDS = [f"dolly_{i}" for i in range(500)]


def pt(rouge, div, sd_r=3.0, sd_d=0.05, seed=0):
    rng = np.random.default_rng(seed)
    def z(sd):
        x = rng.normal(0, sd, 500)
        return x - x.mean()
    return {"ids": IDS, "rougeL_sampled": rouge + z(sd_r), "semantic_div": div + z(sd_d),
            "self_bleu": 10 + z(1), "teacher_lp_tok": -3 + z(0.2), "ended": 0.6 + z(0.0), "len": 150 + z(10)}


def pair(dr, dd, seeds=(1, 2)):
    """top-8 = full + (dr, dd) with prompt-level noise that is shared across arms (paired design)."""
    x, y = {}, {}
    for s in (1, 2):
        if s not in seeds:
            x[s] = y[s] = None
            continue
        base = pt(15, 0.30, seed=s)
        y[s] = base
        rng = np.random.default_rng(100 + s)
        e_r, e_d = rng.normal(0, 1.0, 500), rng.normal(0, 0.02, 500)
        x[s] = {**base, "rougeL_sampled": base["rougeL_sampled"] + dr + e_r - e_r.mean(),
                "semantic_div": base["semantic_div"] + dd + e_d - e_d.mean()}
    return x, y


def test_selection_rule():
    v = {1.0: 20.0, 1.15: 19.0, 1.3: 18.0, 1.5: 17.0}
    assert R.extension(v, 18.4) is None and R.choose(v, 18.4) == 1.3
    assert R.extension(v, 16.5) == 1.65                       # below the range: step beyond T = 1.5
    assert R.extension(v, 21.0) == 0.85                       # above the range: step below T = 1.0
    assert R.extension({0.7: 15, 0.85: 14, 1.0: 13}, 16) == 0.55
    assert R.extension({0.15: 15, 0.3: 14}, 16) is None       # never T <= 0
    assert R.choose(v, 18.5) == 1.15                          # tie -> closer to 1.0
    assert R.choose(v, 10.0) == 1.5                           # no extrapolation: closest measured


def test_recovered_is_b():
    r = M.compare(*pair(0.0, 0.0))
    assert r["verdict"] == "b", r["metrics"]["semantic_div"]
    assert r["complete"]


def test_residual_deficit_is_a():
    r = M.compare(*pair(0.0, -0.04))
    assert r["verdict"] == "a", r["metrics"]["semantic_div"]
    assert abs(r["metrics"]["semantic_div"]["effect"] + 0.04) < 0.005


def test_quality_not_equivalent_is_inconclusive():
    assert M.compare(*pair(3.0, -0.04))["verdict"] == "inconclusive"


def test_diversity_between_margin_and_zero_is_inconclusive():
    # effect -0.010 exactly: CI straddles -margin -> neither (a) nor (b)
    assert M.compare(*pair(0.0, -0.010))["verdict"] == "inconclusive"


def test_sensitivity_and_partial_seeds():
    r = M.compare(*pair(0.0, -0.012), b=2000)
    assert set(r["sensitivity"]) == {"0.005", "0.015"}
    r1 = M.compare(*pair(0.0, 0.0, seeds=(1,)))
    assert r1["seeds"] == [1] and not r1["complete"]
    r0 = M.compare(*pair(0.0, 0.0, seeds=()))
    assert not r0["available"]


def test_headline_rule():
    assert M.headline("a", "a") == "a" and M.headline("b", "inconclusive") == "b"
    assert M.headline("a", "b") == "inconclusive" and M.headline("inconclusive", "a") == "inconclusive"


def test_nan_prompts_dropped_not_crash():
    x, y = pair(0.0, 0.0)
    x[1]["semantic_div"][:5] = np.nan
    r = M.compare(x, y, b=1000)
    assert r["metrics"]["semantic_div"]["n_prompts_missing"] == 5 and r["verdict"] == "b"


def test_all_missing_is_inconclusive_not_b():
    x, y = pair(0.0, 0.0)
    for s in (1, 2):
        x[s]["semantic_div"][:] = np.nan
    r = M.compare(x, y, b=500)
    assert r["verdict"] == "inconclusive" and r["metrics"]["semantic_div"]["effect"] is None


def test_ids_aligned_by_value():
    x, y = pair(0.0, -0.04)
    perm = np.random.default_rng(3).permutation(500)
    for s in (1, 2):
        y[s] = {k: (np.asarray(v)[perm] if k != "ids" else [v[i] for i in perm]) for k, v in y[s].items()}
    assert M.compare(x, y, b=2000)["verdict"] == "a"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("ALL FINAL TESTS PASSED")
