"""Each metric against k (top-k and tail bucket, FKL and RKL), with full KD / SFT / RS / teacher as
horizontal reference lines (brief: "plot each metric against k, with full KD as a reference line").

  python analysis/plots.py [--split valid] [--student 135m] [--n all]   -> $CF_ROOT/figures/*.pdf
"""
import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cachekd.config import CONFIG_VERSION  # noqa: E402
from cachekd.utils import path, read_jsonl  # noqa: E402

METRICS = {"semantic_div": "semantic diversity (mean pairwise cosine distance)", "distinct2": "distinct-2",
           "self_bleu": "Self-BLEU (lower = more diverse)", "rougeL_greedy": "ROUGE-L (greedy)",
           "tok_entropy_ratio": "student / teacher entropy", "tok_tail_cov_top8": "teacher tail mass covered (outside top-8)",
           "tok_ece": "next-token ECE", "recall_student_lp": "recall: student log-lik of teacher samples",
           "precision_teacher_lp": "precision: teacher log-lik of student samples"}
STYLE = {"fkl": dict(color="#1f77b4", marker="o"), "rkl": dict(color="#d62728", marker="s")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="valid")
    ap.add_argument("--student", default="135m")
    ap.add_argument("--n", default="all")
    a = ap.parse_args()
    tag = f"_{a.student}_n{a.n}_"
    rows = [r for r in read_jsonl(path("runs", "evals.jsonl"))
            if r["split"] == a.split and r.get("config_version") == CONFIG_VERSION
            and (tag in r["run_id"] + "_" or r["run_id"] == "teacher")]
    out = path("figures", "x").parent
    for m, label in METRICS.items():
        fig, ax = plt.subplots(figsize=(4.2, 3.2))
        for div in ("fkl", "rkl"):
            for arm, ls in (("topk", "-"), ("tailbucket", "--")):
                ks = sorted({int(r["run_id"].split("_k")[1].split("_")[0]) for r in rows
                             if r["run_id"].startswith(arm + "_k") and f"_{div}_" in r["run_id"]})
                ys = [[r.get(m, np.nan) for r in rows if r["run_id"].startswith(f"{arm}_k{k}_{div}_")] for k in ks]
                if ks:
                    ax.errorbar(ks, [np.nanmean(y) for y in ys], yerr=[np.nanstd(y, ddof=1) if len(y) > 1 else 0 for y in ys],
                                ls=ls, capsize=2, label=f"{arm} {div.upper()}", **STYLE[div])
            for ref, ls in (("full", "-"), ("rs", ":")):
                v = [r.get(m, np.nan) for r in rows if r["run_id"].startswith(f"{ref}_{div}_")]
                if v:
                    ax.axhline(np.nanmean(v), ls=ls, color=STYLE[div]["color"], alpha=.6, label=f"{ref} {div.upper()}")
        for ref, c in (("sft", "gray"), ("teacher", "black")):
            v = [r.get(m, np.nan) for r in rows if r["run_id"] == ref or r["run_id"].startswith(ref + "_")]
            if v and not np.all(np.isnan(v)):
                ax.axhline(np.nanmean(v), ls="-.", color=c, alpha=.7, label=ref)
        ax.set_xscale("log", base=2)
        ax.set_xlabel("k (cached teacher probabilities)")
        ax.set_ylabel(label, fontsize=8)
        ax.legend(fontsize=6)
        fig.tight_layout()
        fig.savefig(out / f"{m}{tag}{a.split}.pdf")
        plt.close(fig)
    print(f"figures -> {out}")


if __name__ == "__main__":
    main()
