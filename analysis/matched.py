"""Matched-temperature comparisons A and B: the analysis pre-registered in EXPERIMENT_LOG.md (19 Oct 14:05).

A (primary):   top-8 @ T_top8* vs full KD @ 1.0      B (secondary): top-8 @ 1.0 vs full KD @ T_full*
Effect = top-8 minus full KD, Dolly test, training seeds 1 and 2. Per prompt: the per-prompt value (mean over its
8 samples, or its 8-sample block for diversity), averaged over the seeds present for BOTH arms. Paired
prompt-level bootstrap over prompt IDs (B = 10,000, rng seed 0), 95% percentile intervals.
Verdict: quality ("reference overlap" = sampled ROUGE-L) equivalent := CI inside [-1.0, +1.0];
  (a) residual deficit      quality equivalent AND semantic-diversity CI entirely below -margin
  (b) recovered by temperature  quality equivalent AND diversity CI entirely inside [-margin, +margin]
  else inconclusive.  margin = 0.010 (sensitivity 0.005, 0.015).
Headline = A's verdict, unless B gives the opposite conclusive verdict -> inconclusive.

Usage: python analysis/matched.py   (prints; make_results.py imports `analyse`)
"""
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from cachekd.config import CFG, CONFIG_VERSION  # noqa: E402
from cachekd.utils import path, read_jsonl  # noqa: E402

Q_MARGIN = 1.0
D_MARGIN = 0.010
D_SENS = (0.005, 0.015)
B = 10_000
SEEDS = (1, 2)
METRICS = ("rougeL_sampled", "semantic_div", "self_bleu", "teacher_lp_tok", "ended", "len")


def _rid(arm: str, seed: int) -> str:
    from cachekd import protocol as P
    from cachekd.config import run_id
    return (run_id("topk", "fkl", 8, P.STUDENT, None, seed, "cache") if arm == "top8"
            else run_id("full", "fkl", None, P.STUDENT, None, seed, "online"))


def load_point(rid: str, T: float, split: str = "test"):
    """Verified per-prompt arrays of one (run, T) point on the full eval set, or None."""
    from cachekd.tempsweep import perprompt_file, verified
    n = CFG["eval_prompts"]
    best = None
    for r in read_jsonl(path("runs", "tempsweep.jsonl")):
        if (r["run_id"] == rid and r["split"] == split and round(float(r["temperature"]), 6) == round(float(T), 6)
                and r.get("n_prompts") == n and r.get("config_version") == CONFIG_VERSION
                and r.get("n_samples") == CFG["n_samples"] and r.get("max_new_tokens") == CFG["max_new_tokens"]
                and r.get("seed", 0) == 0):
            f = perprompt_file(rid, split, r["temperature"], n)
            if verified(r, f):
                best = (r, f)
    if best is None:
        return None
    d = json.loads(best[1].read_text(encoding="utf-8"))
    return {"row": best[0], "ids": d["ids"], **{m: np.array(d[m], dtype=float) for m in METRICS if m in d}}


def verdict(q_ci, d_ci, margin: float = D_MARGIN) -> str:
    if q_ci is None or d_ci is None or not np.all(np.isfinite(list(q_ci) + list(d_ci))):
        return "inconclusive"
    eq = q_ci[0] >= -Q_MARGIN and q_ci[1] <= Q_MARGIN
    if eq and d_ci[1] < -margin:
        return "a"
    if eq and d_ci[0] >= -margin and d_ci[1] <= margin:
        return "b"
    return "inconclusive"


def headline(va: str, vb: str) -> str:
    if va in ("a", "b") and vb in ("a", "b") and va != vb:
        return "inconclusive"
    return va


def compare(x_pts: dict, y_pts: dict, b: int = B, rng_seed: int = 0) -> dict:
    """x_pts / y_pts: {seed: point or None} for top-8 / full KD. Paired over prompt IDs and seeds."""
    seeds = [s for s in sorted(x_pts) if x_pts.get(s) is not None and y_pts.get(s) is not None]
    if not seeds:
        return {"seeds": [], "available": False}
    ids = list(x_pts[seeds[0]]["ids"])
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("prompt IDs must be non-empty and unique")
    for s in seeds:  # every point must cover exactly the same prompts (aligned by ID below)
        for pt in (x_pts[s], y_pts[s]):
            if sorted(pt["ids"]) != sorted(ids):
                raise ValueError(f"prompt IDs differ between points (seed {s})")
    out = {"seeds": seeds, "available": True, "n_prompts": len(ids), "metrics": {}}
    rng = np.random.default_rng(rng_seed)

    def aligned(pt, m):
        a = np.asarray(pt[m], dtype=float)
        if a.shape != (len(ids),):
            raise ValueError(f"{m}: expected one value per prompt, got shape {a.shape}")
        pos = {i: j for j, i in enumerate(pt["ids"])}
        return a[[pos[i] for i in ids]]
    for m in METRICS:
        if not all(m in x_pts[s] and m in y_pts[s] for s in seeds):
            continue
        X = np.stack([aligned(x_pts[s], m) for s in seeds])  # (seeds, prompts)
        Y = np.stack([aligned(y_pts[s], m) for s in seeds])
        ok = np.isfinite(X).all(0) & np.isfinite(Y).all(0)  # complete cases: finite in both arms and all seeds
        n_ok = int(ok.sum())
        if n_ok < 2:
            out["metrics"][m] = {"x": None, "y": None, "effect": None, "ci": None, "n_prompts_used": n_ok,
                                 "n_prompts_missing": int((~ok).sum()), "per_seed": {}}
            continue
        Xo, Yo = X[:, ok], Y[:, ok]
        diff = (Xo - Yo).mean(0)  # per prompt, seed-averaged paired difference
        boots = diff[rng.integers(0, n_ok, (b, n_ok))].mean(1)
        lo, hi = np.percentile(boots, [2.5, 97.5])
        out["metrics"][m] = {"x": float(Xo.mean()), "y": float(Yo.mean()), "effect": float(diff.mean()),
                             "ci": [float(lo), float(hi)], "n_prompts_used": n_ok, "n_prompts_missing": int((~ok).sum()),
                             "per_seed": {str(s): float((Xo[i] - Yo[i]).mean()) for i, s in enumerate(seeds)}}
    q = (out["metrics"].get("rougeL_sampled") or {}).get("ci")
    d = (out["metrics"].get("semantic_div") or {}).get("ci")
    out["verdict"] = verdict(q, d)
    out["sensitivity"] = {f"{mg:.3f}": verdict(q, d, mg) for mg in D_SENS}
    out["complete"] = seeds == list(SEEDS)
    return out


def analyse(selection: dict | None = None) -> dict:
    sel_f = path("runs", "final_selection.json")
    if selection is None:
        if not sel_f.exists():
            return {"status": "no selection yet", "headline": "pending"}
        selection = json.loads(sel_f.read_text(encoding="utf-8"))
    t8, tf = selection["T_top8"], selection["T_full"]
    res = {"selection": selection}
    for name, (Tx, Ty) in {"A": (t8, 1.0), "B": (1.0, tf)}.items():
        x = {s: load_point(_rid("top8", s), Tx) for s in SEEDS}
        y = {s: load_point(_rid("full", s), Ty) for s in SEEDS}
        r = compare(x, y)
        r.update({"T_top8": Tx, "T_full": Ty})
        res[name] = r
    va = res["A"]["verdict"] if res["A"].get("available") else "pending"
    vb = res["B"]["verdict"] if res["B"].get("available") else "pending"
    complete = bool(res["A"].get("complete") and res["B"].get("complete"))
    # the registered verdict needs both seeds of both comparisons; before that only an interim verdict exists
    res["interim_headline"] = headline(va, vb) if va != "pending" else "pending"
    res["headline"] = res["interim_headline"] if complete else "pending"
    res["provisional"] = not complete
    return res


def main() -> None:
    r = analyse()
    out = path("runs", "matched.json")
    out.write_text(json.dumps(r, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in r.items() if k != "selection"}, indent=1)[:6000])
    print(f"\nHEADLINE: {r['headline']} (interim, from the seeds present: {r.get('interim_headline')})")


if __name__ == "__main__":
    main()
