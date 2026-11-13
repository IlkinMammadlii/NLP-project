"""Score the blinded human audit of comparison A (run by the authors AFTER rating; needs only numpy).

    python audit/score_audit.py            (from the repo root, or anywhere)

Inputs (written once by make_results.py; never regenerated, so your ratings are safe):
  audit/rating_sheet.csv  200 rows = 50 test prompts x 2 conditions x 2 samples, shuffled, condition hidden.
                          Column `raters`: "r1" -> only rater 1 fills r1_*; "r2" -> only rater 2 fills r2_*;
                          "both" (20 prompts = 80 rows) -> both raters fill their columns independently.
                          follows / coherent: 1, 2 or 3 (3 = best); correct: yes / no / na (na = not checkable).
  audit/key.csv           item_id -> condition (top8 at T*, full KD at 1.0). Do not open it before rating.
Outputs: per-condition means, paired top-8 minus full differences with 95% prompt-bootstrap intervals,
inter-rater agreement on the double-rated prompts, and tables/audit.tex (the paper inputs it).
"""
import csv
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DIMS = [("follows", "Follows the instruction (1--3)"), ("correct", "Correct where checkable (\\% yes)"),
        ("coherent", "Coherent (1--3)")]


def num(dim: str, v: str):
    v = (v or "").strip().lower()
    if dim == "correct":
        return {"yes": 1.0, "y": 1.0, "1": 1.0, "no": 0.0, "n": 0.0, "0": 0.0}.get(v)  # na / blank -> None
    try:
        x = float(v)
    except ValueError:
        return None
    return x if x in (1.0, 2.0, 3.0) else None


def kappa(a: list, b: list, cats: list, weighted: bool) -> float:
    """Cohen's kappa (quadratic weights if `weighted`)."""
    k = len(cats)
    idx = {c: i for i, c in enumerate(cats)}
    o = np.zeros((k, k))
    for x, y in zip(a, b):
        o[idx[x], idx[y]] += 1
    n = o.sum()
    if n == 0:
        return float("nan")
    e = np.outer(o.sum(1), o.sum(0)) / n
    w = (np.subtract.outer(np.arange(k), np.arange(k)) ** 2 / (k - 1) ** 2) if weighted else 1 - np.eye(k)
    den = (w * e).sum()
    return float(1 - (w * o).sum() / den) if den > 0 else float("nan")


def main() -> int:
    sheet, key = HERE / "rating_sheet.csv", HERE / "key.csv"
    if not sheet.exists() or not key.exists():
        print("audit/rating_sheet.csv or audit/key.csv missing: make_results.py writes them once comparison A exists.")
        return 1
    cond = {r["item_id"]: r for r in csv.DictReader(open(key, encoding="utf-8"))}
    rows = list(csv.DictReader(open(sheet, encoding="utf-8-sig")))
    per_item = {}  # item -> dim -> mean over the raters who rated it
    pairs = {d: ([], []) for d, _ in DIMS}
    missing = 0
    for r in rows:
        iid = r["item_id"]
        per_item[iid] = {}
        for d, _ in DIMS:
            v1 = num(d, r.get(f"r1_{d}_1to3" if d != "correct" else "r1_correct_yes_no_na", ""))
            v2 = num(d, r.get(f"r2_{d}_1to3" if d != "correct" else "r2_correct_yes_no_na", ""))
            vals = [v for v in (v1, v2) if v is not None]
            per_item[iid][d] = float(np.mean(vals)) if vals else None
            if r["raters"] == "both" and v1 is not None and v2 is not None:
                pairs[d][0].append(v1)
                pairs[d][1].append(v2)
            if d != "correct" and not vals:
                missing += 1
    if missing:
        print(f"note: {missing} required ratings (follows/coherent) are blank; they are left out.")
    # per prompt and condition: mean over its 2 samples
    by = {}
    for iid, vals in per_item.items():
        c = cond[iid]
        for d, _ in DIMS:
            if vals[d] is not None:
                by.setdefault((c["prompt_id"], c["condition"], d), []).append(vals[d])
    prompts = sorted({c["prompt_id"] for c in cond.values()})
    rng = np.random.default_rng(0)
    res = {}
    for d, _ in DIMS:
        x = np.array([np.mean(by[(p, "top8", d)]) if (p, "top8", d) in by else np.nan for p in prompts])
        y = np.array([np.mean(by[(p, "full", d)]) if (p, "full", d) in by else np.nan for p in prompts])
        ok = np.isfinite(x) & np.isfinite(y)
        diff = (x - y)[ok]
        scale = 100.0 if d == "correct" else 1.0
        if len(diff) >= 2:
            boots = diff[rng.integers(0, len(diff), (10000, len(diff)))].mean(1)
            lo, hi = np.percentile(boots, [2.5, 97.5])
        else:
            lo = hi = float("nan")
        res[d] = {"top8": scale * np.nanmean(x) if np.isfinite(x).any() else float("nan"),
                  "full": scale * np.nanmean(y) if np.isfinite(y).any() else float("nan"),
                  "diff": scale * diff.mean() if len(diff) else float("nan"), "lo": scale * lo, "hi": scale * hi,
                  "n": int(ok.sum())}
    agree = {}
    for d, _ in DIMS:
        a, b = pairs[d]
        if a:
            cats = [0.0, 1.0] if d == "correct" else [1.0, 2.0, 3.0]
            agree[d] = {"n": len(a), "exact": 100 * float(np.mean(np.array(a) == np.array(b))),
                        "kappa": kappa(a, b, cats, weighted=d != "correct")}
        else:
            agree[d] = {"n": 0, "exact": float("nan"), "kappa": float("nan")}
    T = next((c["temperature"] for c in cond.values() if c["condition"] == "top8"), "?")
    print(f"Comparison A: top-8 at T={T} vs full KD at T=1.0, {len(prompts)} prompts")
    for d, lab in DIMS:
        r = res[d]
        print(f"  {d:9s} top-8 {r['top8']:.2f}  full {r['full']:.2f}  diff {r['diff']:+.2f} [{r['lo']:+.2f}, {r['hi']:+.2f}]"
              f"  (n={r['n']} prompts)   agreement: {agree[d]['exact']:.0f}% exact, kappa {agree[d]['kappa']:.2f} "
              f"(n={agree[d]['n']} double-rated items)")
    f = lambda v, p=2: "n/a" if not np.isfinite(v) else f"{v:.{p}f}"  # noqa: E731
    lines = ["% generated by audit/score_audit.py", "\\begin{table}[t]\\centering\\small", "\\resizebox{\\columnwidth}{!}{%",
             "\\begin{tabular}{l ccc c}", "\\toprule",
             f"Dimension & Top-8 ($T{{=}}{T}$) & Full KD & Diff.\\ [95\\% CI] & Agreement ($\\kappa$) \\\\", "\\midrule"]
    for d, lab in DIMS:
        r, a = res[d], agree[d]
        p = 1 if d == "correct" else 2
        lines.append(f"{lab} & {f(r['top8'], p)} & {f(r['full'], p)} & {f(r['diff'], p)} [{f(r['lo'], p)}, {f(r['hi'], p)}] & "
                     f"{f(a['exact'], 0)}\\% ({f(a['kappa'])}) \\\\")
    lines += ["\\bottomrule", "\\end{tabular}}",
              f"\\caption{{Blinded human audit of comparison A: {len(prompts)} Dolly-test prompts $\\times$ 2 conditions "
              "$\\times$ 2 samples, condition hidden; per prompt the mean of its two samples (and of both raters where "
              "double-rated); paired prompt bootstrap. Agreement on the 20 double-rated prompts: exact agreement and "
              "Cohen's $\\kappa$ (quadratic weights for the 1--3 scales).}", "\\label{tab:audit}", "\\end{table}"]
    out = REPO / "tables" / "audit.tex"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out}; rebuild the paper with: bash build_paper.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
