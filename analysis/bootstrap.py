"""Paired differences between arms with uncertainty from BOTH prompts and training seeds (CPU only; safe
while the grid runs).

For every arm vs full KD on the same Dolly-test prompts, per-prompt values come from the saved generations:
greedy ROUGE-L, mean sampled ROUGE-L of the 8 samples, Self-BLEU and semantic diversity (0-100 scale for
ROUGE-L and Self-BLEU; empty samples: ROUGE-L 0, skipped by Self-BLEU and semantic diversity - the empty rate
is reported per evaluation). Seed s of the arm is paired with seed s of the reference. Reported per metric:
  - per seed: mean difference and 95% CI from resampling prompts (B = 10,000);
  - two-level: each replicate resamples seeds (with replacement, from the seeds both arms have) AND prompts,
    so the interval includes training-seed variance; with 2-3 seeds this interval is coarse and is the one
    used for claims;
  - seed level: mean ± std of the per-seed mean differences.
Greedy ROUGE-L is called "equivalent" only if the two-level 90% CI lies inside ±EQ_MARGIN = 1.0 ROUGE-L
point (two one-sided tests at alpha = 0.05; percentile interval, approximate). These are estimates with
intervals, not a family of hypothesis tests; the declared main contrast is top-8 vs full KD.
Per-prompt values are cached under runs/perprompt/ with a fingerprint of their inputs (generation file
size + mtime, prompt ids, metric version) and rebuilt when it changes. Output: runs/bootstrap.jsonl.

  .venv\\Scripts\\python.exe analysis\\bootstrap.py [--split test] [--reference full_fkl]
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cachekd import data as D  # noqa: E402
from cachekd import metrics as M  # noqa: E402
from cachekd.config import MODELS  # noqa: E402

ROOT = Path(os.environ.get("CF_ROOT", Path(__file__).resolve().parent.parent / "cf_data"))
B, EQ_MARGIN, METRIC_VERSION = 10_000, 1.0, 1
METRICS = ["rougeL_greedy", "rougeL_sampled", "self_bleu", "semantic_div"]
ARMS = ["topk_k8_fkl", "topk_k32_fkl", "topk_k128_fkl", "tailbucket_k8_fkl", "rs_fkl", "sft", "seqkd"]


def jsonl(p: Path) -> list:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").split("\n") if x.strip()] if p.exists() else []


def per_prompt(run: str, split: str, refs: dict, emb) -> dict | None:
    """{prompt id: {metric: value}} for one evaluation, or None if its generations are missing/incomplete."""
    g = ROOT / "gens" / f"{run}__{split}__n{len(refs)}__s0.jsonl"
    if not g.exists():
        return None
    st = g.stat()
    fp = hashlib.md5(json.dumps([st.st_size, int(st.st_mtime), sorted(refs), METRIC_VERSION,
                                 MODELS["embedder"]]).encode()).hexdigest()
    cache = ROOT / "runs" / "perprompt" / f"{run}__{split}.json"
    if cache.exists():
        c = json.loads(cache.read_text(encoding="utf-8"))
        if c.get("fingerprint") == fp:
            return c["values"]
    rows = jsonl(g)
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)) or set(ids) != set(refs):
        print(f"skip {run}: generations do not cover exactly the {len(refs)} prompts")
        return None
    flat = [s for r in rows for s in r["samples"]]
    vecs = emb.encode(flat, batch_size=64, convert_to_numpy=True, show_progress_bar=False)
    out, i = {}, 0
    for r in rows:
        k = len(r["samples"])
        v = np.stack([x for x, s in zip(vecs[i:i + k], r["samples"]) if s.strip()] or [np.zeros(vecs.shape[1])])
        i += k
        out[r["id"]] = {"rougeL_greedy": M.rouge_l(r["greedy"], refs[r["id"]]),
                        "rougeL_sampled": float(np.mean([M.rouge_l(s, refs[r["id"]]) for s in r["samples"]])),
                        "self_bleu": M.self_bleu(r["samples"]), "semantic_div": M.semantic_diversity(v)}
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fingerprint": fp, "values": out}), encoding="utf-8")
    os.replace(tmp, cache)
    return out


def prompt_ci(d: np.ndarray, rng) -> tuple:
    d = d[~np.isnan(d)]
    means = d[rng.integers(0, len(d), size=(B, len(d)))].mean(1)
    return float(d.mean()), np.percentile(means, [2.5, 97.5]).tolist()


def two_level(D_: np.ndarray, rng) -> tuple:
    """D_: (seeds, prompts) per-prompt differences, prompts aligned by id. Resample seeds and prompts."""
    S, P = D_.shape
    est = np.empty(B)
    for b in range(B):
        s = rng.integers(0, S, size=S)
        p = rng.integers(0, P, size=P)
        est[b] = np.nanmean(D_[np.ix_(s, p)])
    return float(np.nanmean(D_)), np.percentile(est, [2.5, 97.5]).tolist(), np.percentile(est, [5, 95]).tolist()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="test")
    ap.add_argument("--reference", default="full_fkl")
    a = ap.parse_args()
    import torch
    torch.set_num_threads(4)
    from sentence_transformers import SentenceTransformer
    emb = SentenceTransformer(MODELS["embedder"], device="cpu")
    rows = D.load_eval_set(a.split, 500)
    refs = {r["id"]: r["refs"] for r in rows}
    ids = sorted(refs)
    rng = np.random.default_rng(0)
    out = []
    for arm in ARMS:
        diffs = {}  # seed -> {metric: array over `ids`}
        for seed in (1, 2, 3):
            x = per_prompt(f"{arm}_135m_nall_s{seed}_v1", a.split, refs, emb)
            y = per_prompt(f"{a.reference}_135m_nall_s{seed}_v1", a.split, refs, emb)
            if x and y:
                diffs[seed] = {m: np.array([x[i][m] - y[i][m] for i in ids], dtype=float) for m in METRICS}
        if not diffs:
            continue
        for m in METRICS:
            per_seed = {s: prompt_ci(d[m], rng) for s, d in diffs.items()}
            est, ci95, ci90 = two_level(np.stack([d[m] for d in diffs.values()]), rng)
            seed_means = [v[0] for v in per_seed.values()]
            rec = {"arm": arm, "reference": a.reference, "split": a.split, "metric": m, "seeds": sorted(diffs),
                   "n_prompts": len(ids), "diff": est, "two_level_ci95": ci95, "two_level_ci90": ci90,
                   "per_seed": {str(s): {"diff": v[0], "prompt_ci95": v[1]} for s, v in per_seed.items()},
                   "seed_mean": float(np.mean(seed_means)),
                   "seed_std": float(np.std(seed_means, ddof=1)) if len(seed_means) > 1 else None}
            if m == "rougeL_greedy":
                rec["eq_margin"] = EQ_MARGIN
                rec["equivalent_within_margin"] = bool(-EQ_MARGIN < ci90[0] and ci90[1] < EQ_MARGIN)
            out.append(rec)
            eq = f"  equivalent within ±{EQ_MARGIN}: {rec['equivalent_within_margin']}" if m == "rougeL_greedy" else ""
            sd = f" (seed sd {rec['seed_std']:.3f})" if rec["seed_std"] is not None else ""
            print(f"{arm:18s} {m:15s} seeds {rec['seeds']}  diff {est:+.3f}  two-level 95% CI "
                  f"[{ci95[0]:+.3f}, {ci95[1]:+.3f}]{sd}{eq}", flush=True)
    (ROOT / "runs" / "bootstrap.jsonl").write_text("\n".join(json.dumps(r) for r in out) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
