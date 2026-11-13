"""Quality-diversity frontier from the temperature sweep, with the pre-specified decision rule (CPU only; reads
result files). Analysis plan, margins and both outcomes: HYPOTHESES.md ("Temperature-sweep analysis plan",
committed before any point was run, plus the analysis decisions added after code review and before any data).

Per arm: one curve (temperature in increasing order) of per-prompt means over the same 500 Dolly-test prompts.
Quality axes (decision): teacher log-likelihood per token of the samples (PRIMARY, reference-free; per-prompt mean
of the per-sample means) and sampled ROUGE-L (secondary). Sensitivity only (never decides): the token-weighted
teacher log-likelihood. Diversity: semantic diversity (PRIMARY, margin 0.01) and Self-BLEU (secondary, margin 1.0).
Read-off: D = diversity(arm) - diversity(full KD at equal quality), oriented so that negative = arm LESS diverse;
full KD interpolated linearly on adjacent-temperature segments (first geometric crossing in increasing T; no
fallback to a later crossing when its diversity is undefined; multiple crossings are counted and reported).
Registered statistic = mean of D over the set J of top-8 points whose point-estimate quality lies strictly inside
full KD's measured quality range. Uncertainty: paired prompt-level bootstrap (B = 10,000; the SAME prompt resample
for every arm and temperature; curves and interpolation recomputed in every replicate). A replicate counts only if
EVERY point in J has a crossing (complete-J); if fewer than 90% of replicates are complete, the overlap is not
stable under resampling and the outcome is "not determined". The interval is conditional on complete-J
replicates (documented). One training seed: the interval does not cover training-seed variance.
Confirmatory output needs the complete registered core grid (8 verified points); otherwise everything is printed
as PROVISIONAL and no decision is issued. RS-KD and tail bucket are read the same way, as exploratory.
Artifacts are verified (sha256 of the per-prompt file == the result row's hash; embedded setup == the row's setup;
500 unique prompt ids; every array length 500; n_samples 8; max_new_tokens 256). Missing scores: +-inf and NaN are
missing; every mean is over finite values; missing counts are printed and flagged above 2%.

  .venv\\Scripts\\python.exe analysis\\frontier.py   -> $CF_ROOT/figures/frontier_test.{pdf,png}, $CF_ROOT/runs/frontier.json
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cachekd import sweepplan  # noqa: E402
from cachekd.config import CONFIG_VERSION  # noqa: E402

ROOT = Path(os.environ.get("CF_ROOT", Path(__file__).resolve().parent.parent / "cf_data"))
B = 10_000
COVERAGE_MIN, MISSING_WARN = 0.90, 0.02
EXPECTED = {"split": "test", "seed": 0, "n_prompts": 500, "n_samples": 8, "max_new_tokens": 256,
            "config_version": CONFIG_VERSION}
ARRAYS = ("rougeL_sampled", "semantic_div", "self_bleu", "teacher_lp", "teacher_lp_tok", "len", "ended")
REF = "full_fkl_135m_nall_s1_v1"
PRIMARY = "topk_k8_fkl_135m_nall_s1_v1"
ARMS = {PRIMARY: ("top-8 cache", "#d62728", "o"), REF: ("full KD", "#1f77b4", "s"),
        "rs_fkl_135m_nall_s1_v1": ("RS-KD (exploratory)", "#2ca02c", "^"),
        "tailbucket_k8_fkl_135m_nall_s1_v1": ("tail bucket k=8 (exploratory)", "#ff7f0e", "D")}
QUALITY = {"teacher_lp": "teacher log-lik per token, prompt-mean (reference-free; higher = better)",
           "rougeL_sampled": "sampled ROUGE-L (mean of 8; higher = better)"}
SENSITIVITY = {"teacher_lp_tok": "teacher log-lik per token, token-weighted (sensitivity)"}
DIV = {"semantic_div": ("semantic diversity (higher = more varied)", +1, 0.01),
       "self_bleu": ("Self-BLEU (lower = more varied)", -1, 1.0)}
TEXT = {"A": "ON THE CURVE: top-k shifts the student along the frontier (sharper), largely recoverable by temperature "
             "(scope: the registered average over matched-quality points, this checkpoint pair, one training seed).",
        "B": "BELOW: top-k loses diversity at equal quality.",
        "C": "ABOVE: top-8 is MORE diverse at equal quality (reported as such; not support for the hypothesis).",
        "inconclusive": "INCONCLUSIVE: the interval straddles a margin or the quality axes disagree; report both "
                        "frontiers, no headline claim.",
        "not determined": "NOT DETERMINED: on at least one quality axis there is no (stable) overlap in quality; "
                          "pre-specified fallback = add temperatures (log it)."}


def jsonl(p: Path) -> list:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").split("\n") if x.strip()] if p.exists() else []


def load():
    """(rows, pts, bad): rows = result rows of the registered setup; pts = {(run, T): arrays} for rows whose
    per-prompt artifact verifies; bad = [(key, reason)] for the rest."""
    rows = {}
    for r in jsonl(ROOT / "runs" / "tempsweep.jsonl"):
        if all(r.get(k) == v for k, v in EXPECTED.items()):
            rows[(r["run_id"], round(float(r["temperature"]), 6))] = r  # newest wins
    pts, bad, ids = {}, [], None
    for key, r in sorted(rows.items()):
        run, T = key
        f = ROOT / "runs" / "sweep_perprompt" / f"{run}__test__T{T:g}.json"
        problem = None
        if not f.exists():
            problem = "per-prompt file missing"
        else:
            raw = f.read_bytes()
            if hashlib.sha256(raw).hexdigest() != r.get("perprompt_sha256"):
                problem = "per-prompt file does not match the recorded sha256"
            else:
                d = json.loads(raw)
                if d.get("setup") != {k: r[k] for k in ("run_id", "split", "temperature", "seed", "n_prompts",
                                                        "n_samples", "max_new_tokens", "config_version")}:
                    problem = "embedded setup differs from the result row"
                elif len(d["ids"]) != 500 or len(set(d["ids"])) != 500:
                    problem = "prompt ids are not 500 unique ids"
                elif any(len(d[k]) != 500 for k in ARRAYS):
                    problem = "an array does not have 500 entries"
                elif ids is not None and d["ids"] != ids:
                    problem = "prompt ids differ from the other points"
        if problem is None:
            ids = ids or d["ids"]
            arr = {k: np.array(d[k], dtype=float) for k in ARRAYS}
            for a in arr.values():
                a[~np.isfinite(a)] = np.nan
            if not all(np.isfinite(np.nanmean(arr[k])) if np.isfinite(arr[k]).any() else False
                       for k in ("rougeL_sampled", "semantic_div", "self_bleu", "teacher_lp", "teacher_lp_tok")):
                problem = "a point mean is not finite (all values missing)"
            else:
                pts[key] = arr
        if problem:
            bad.append((key, problem))
    return rows, pts, bad


def interp_first(qf, df, qt):
    """Full KD's diversity at quality qt: FIRST geometric crossing on adjacent segments (increasing T); if its
    diversity is undefined the result stays NaN (no fallback to a later crossing). qf, df: (B, nF); qt: (B,).
    Returns (value (B,), number of distinct crossing points (B,)); a target at a shared vertex counts once."""
    n = len(qt)
    val, found, cross, prev_ok = np.full(n, np.nan), np.zeros(n, bool), np.zeros(n, int), np.zeros(n, bool)
    for s in range(qf.shape[1] - 1):
        q0, q1, d0, d1 = qf[:, s], qf[:, s + 1], df[:, s], df[:, s + 1]
        lo, hi = np.minimum(q0, q1), np.maximum(q0, q1)
        ok = (qt >= lo) & (qt <= hi) & (hi > lo)
        cross += ok & ~(prev_ok & (qt == q0))
        new = ok & ~found
        val[new] = (d0 + (qt - q0) / np.where(hi > lo, q1 - q0, 1.0) * (d1 - d0))[new]
        found |= ok
        prev_ok = ok
    return val, cross


def pchip_at(qf0, df0, qt0):
    """Sensitivity to the interpolation rule: shape-preserving cubic (PCHIP) through full KD's point estimates
    (a chord underestimates a concave curve, which would bias D upward). NaN outside the range / if not usable."""
    from scipy.interpolate import PchipInterpolator
    o = np.argsort(qf0)
    x, y = qf0[o], df0[o]
    if np.any(np.diff(x) <= 0) or not (x[0] < qt0 < x[-1]):
        return float("nan")
    return float(PchipInterpolator(x, y)(qt0))


def decide(lo, hi, margin):
    if not (np.isfinite(lo) and np.isfinite(hi)):
        return "not determined"
    if -margin < lo and hi < margin:
        return "A"
    if hi < -margin:
        return "B"
    if lo > margin:
        return "C"
    return "inconclusive"


def analyse(pts, arm, idx, quality_keys):
    """{qk|dk: summary} for `arm` against the full-KD curve."""
    Tc = {a: sorted(T for (r, T) in pts if r == a) for a in {r for r, _ in pts}}
    if arm not in Tc or REF not in Tc or len(Tc[REF]) < 2:
        return None
    memo = {}

    def reps(run, T, key):  # (B,) bootstrap means over the shared resample, and the point estimate
        if (run, T, key) not in memo:
            a = pts[(run, T)][key]
            memo[(run, T, key)] = (np.nanmean(a[idx], axis=1), float(np.nanmean(a)))
        return memo[(run, T, key)]

    out = {}
    for qk in quality_keys:
        for dk, (_, orient, margin) in DIV.items():
            qf = np.stack([reps(REF, T, qk)[0] for T in Tc[REF]], 1)
            df = np.stack([reps(REF, T, dk)[0] for T in Tc[REF]], 1)
            qf0 = np.array([reps(REF, T, qk)[1] for T in Tc[REF]])
            df0 = np.array([reps(REF, T, dk)[1] for T in Tc[REF]])
            per_point, Dmat, J = [], [], []
            for T in Tc[arm]:
                qt, qt0 = reps(arm, T, qk)
                dt, dt0 = reps(arm, T, dk)
                v, cross = interp_first(qf, df, qt)
                v0, cross0 = interp_first(qf0[None], df0[None], np.array([qt0]))
                inside = bool(qf0.min() < qt0 < qf0.max())
                D = orient * (dt - v)  # negative = arm LESS diverse at equal quality
                D0 = float(orient * (dt0 - v0[0]))
                defined = np.isfinite(D)
                per_point.append({"T": T, "quality": qt0, "diversity": dt0, "full_diversity_at_equal_quality": float(v0[0]),
                                  "D": D0, "inside_full_range": inside,
                                  "D_pchip_sensitivity": float(orient * (dt0 - pchip_at(qf0, df0, qt0))),
                                  "D_ci95": [float(np.percentile(D[defined], 2.5)), float(np.percentile(D[defined], 97.5))]
                                  if defined.sum() > 100 else None,
                                  "share_replicates_defined": float(defined.mean()),
                                  "multiple_crossings_point_estimate": bool(cross0[0] > 1),
                                  "share_replicates_multiple_crossings": float((cross > 1).mean())})
                if inside:
                    J.append(T)
                    Dmat.append(D)
            res = {"points": per_point, "J": J, "margin": margin, "mean_D": None, "ci95": None,
                   "complete_J_coverage": None, "outcome": "not determined"}
            if Dmat:
                st = np.stack(Dmat, 1)
                complete = np.isfinite(st).all(1)  # the registered statistic is defined only if EVERY J point is
                res["complete_J_coverage"] = float(complete.mean())
                res["mean_D"] = float(np.mean([p["D"] for p in per_point if p["inside_full_range"]]))
                if complete.mean() >= COVERAGE_MIN and complete.sum() > 100 and np.isfinite(res["mean_D"]):
                    Dm = st.mean(1)[complete]
                    res["ci95"] = [float(np.percentile(Dm, 2.5)), float(np.percentile(Dm, 97.5))]
                    res["outcome"] = decide(res["ci95"][0], res["ci95"][1], margin)
            out[f"{qk}|{dk}"] = res
    return out


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, float) and not np.isfinite(o):
        return None
    return o


def main():
    rows, pts, bad = load()
    required = sweepplan.points(sweepplan.PLAN[:sweepplan.CORE_ARMS])
    missing = [k for k in required if k not in pts]
    for key, why in bad:
        print(f"  INVALID point {ARMS.get(key[0], (key[0],))[0]} T={key[1]:g}: {why}")
    runs = {r for r, _ in pts}
    if PRIMARY not in runs or REF not in runs:
        print("temperature sweep not finished yet: points so far",
              {k: sum(1 for (r, _) in pts if r == k) for k in runs})
        return
    provisional = bool(missing)
    if provisional:
        print("PROVISIONAL: the registered core grid is incomplete or invalid; missing: "
              + ", ".join(f"{ARMS[r][0]} T={T:g}" for r, T in missing) + ". No decision is issued.")
    rng = np.random.default_rng(0)
    idx = rng.integers(0, 500, size=(B, 500))  # ONE resample shared by every arm and temperature (paired)
    results = {}
    for arm in ARMS:
        if arm != REF:
            r = analyse(pts, arm, idx, list(QUALITY) + list(SENSITIVITY))
            if r:
                results[arm] = r
    main_ev = {}
    for r in jsonl(ROOT / "runs" / "evals.jsonl"):
        if (r["split"] == "test" and r.get("seed", 0) == 0 and r.get("n_prompts") == 500
                and r.get("config_version") == CONFIG_VERSION):
            main_ev[r["run_id"]] = r
    print("T = 1.0 vs main evaluation (sweep / main; same prompts and sampling seed, up to GPU non-determinism):")
    for run in sorted(runs):
        e, s = main_ev.get(run), rows.get((run, 1.0))
        if e and s and (run, 1.0) in pts:
            print(f"  {ARMS.get(run, (run,))[0]:30s} sampled ROUGE-L {s['rougeL_sampled']:.2f}/{e['rougeL_sampled']:.2f}  "
                  f"semantic {s['semantic_div']:.4f}/{e['semantic_div']:.4f}  Self-BLEU {s['self_bleu']:.2f}/{e['self_bleu']:.2f}  "
                  f"teacher-lp {s['teacher_lp_seq']:.3f}/{e['precision_teacher_lp']:.3f}")
    print("\nPer point: sampled ROUGE-L | teacher lp (sample-wtd, = main eval) / prompt-wtd (analysed) / token-wtd | "
          "semantic | Self-BLEU | finish | length | missing (lp samples, semantic prompts, Self-BLEU prompts)")
    order = lambda kv: (list(ARMS).index(kv[0][0]) if kv[0][0] in ARMS else 9, kv[0][1])  # noqa: E731
    for (run, T), r in sorted(rows.items(), key=order):
        if (run, T) not in pts:
            continue
        miss = (r["n_missing_lp"], r["n_prompts_missing_semantic"], r["n_prompts_missing_self_bleu"])
        flag = "  <-- MISSING > 2%" if (miss[0] / 4000 > MISSING_WARN or miss[1] / 500 > MISSING_WARN
                                        or miss[2] / 500 > MISSING_WARN) else ""
        print(f"  {ARMS.get(run, (run,))[0]:30s} T={T:<5g} {r['rougeL_sampled']:6.2f} | {r['teacher_lp_seq']:7.3f} / "
              f"{r['teacher_lp_prompt']:7.3f} / {r['teacher_lp_tok']:7.3f} | {r['semantic_div']:.3f} | "
              f"{r['self_bleu']:6.2f} | {r['ended_rate_sampled']:.2f} | {r['len_sampled']:.0f} | {miss}{flag}")
    tag0 = "PROVISIONAL " if provisional else ""
    print(f"\n{tag0}Equal-quality read-off (D = oriented diversity gap, negative = arm LESS diverse at equal quality):")
    for arm, res in results.items():
        tag = "PRIMARY" if arm == PRIMARY else "exploratory"
        for key, v in res.items():
            qk, dk = key.split("|")
            sens = " (sensitivity)" if qk in SENSITIVITY else ""
            head = f"  [{tag}] {ARMS[arm][0]:30s} {qk:15s}{sens:14s} {dk:13s}"
            if v["mean_D"] is None:
                print(f"{head} no top-point strictly inside full KD's quality range -> {v['outcome']}")
            elif v["ci95"] is None:
                print(f"{head} J={v['J']} mean D {v['mean_D']:+.4f}  complete-J coverage "
                      f"{v['complete_J_coverage']:.2f} < {COVERAGE_MIN} -> {v['outcome']} (overlap not stable under resampling)")
            else:
                print(f"{head} J={v['J']} mean D {v['mean_D']:+.4f}  95% CI [{v['ci95'][0]:+.4f}, {v['ci95'][1]:+.4f}]  "
                      f"margin ±{v['margin']}  coverage {v['complete_J_coverage']:.2f} -> {v['outcome']}")
    decision = None
    if PRIMARY in results:
        a = results[PRIMARY]["teacher_lp|semantic_div"]["outcome"]
        b = results[PRIMARY]["rougeL_sampled|semantic_div"]["outcome"]
        decision = a if (a == b and a in ("A", "B", "C")) else ("not determined" if "not determined" in (a, b)
                                                                else "inconclusive")
        label = "PROVISIONAL" if provisional else "DECISION"
        print(f"\n{label} (semantic diversity; teacher-lp axis: {a}, sampled-ROUGE-L axis: {b}): "
              f"{decision} -> {TEXT[decision]}")
        if a != b and "not determined" not in (a, b):
            print("  (the two quality axes disagree: report 'mixed' and show both frontiers)")
        g = lambda k: results[PRIMARY][k]["outcome"]  # noqa: E731
        sb = (g("teacher_lp|self_bleu"), g("rougeL_sampled|self_bleu"))
        sens = g("teacher_lp_tok|semantic_div")
        print(f"  secondary (Self-BLEU, margin ±1.0): teacher-lp axis {sb[0]}, sampled-ROUGE-L axis {sb[1]}; "
              f"sensitivity (token-weighted teacher lp, semantic): {sens} -> "
              + ("consistent with the decision." if sens == a == b else "NOT identical to the decision: report it."))
        for key in ("teacher_lp|semantic_div", "rougeL_sampled|semantic_div"):
            v = results[PRIMARY][key]
            if len(v["J"]) == 1:
                print(f"  note ({key}): J has a single point; the summary rests on one temperature.")
            if any(p["inside_full_range"] and p["share_replicates_multiple_crossings"] > 0.05 for p in v["points"]):
                print(f"  note ({key}): >5% of replicates have multiple crossings at a J point (curve folds).")
        print("\n  Primary arm, per point (linear read-off; PCHIP sensitivity), teacher-lp axis, semantic diversity:")
        for p in results[PRIMARY]["teacher_lp|semantic_div"]["points"]:
            print(f"    T={p['T']:<5g} quality {p['quality']:7.3f}  diversity {p['diversity']:.4f}  full KD at equal quality "
                  f"{p['full_diversity_at_equal_quality']:.4f}  D {p['D']:+.4f}  D(PCHIP) {p['D_pchip_sensitivity']:+.4f}  "
                  f"inside {p['inside_full_range']}")
        if provisional:
            decision = None
    (ROOT / "runs" / "frontier.json").write_text(json.dumps(clean({"decision": decision, "provisional": provisional,
                                                                   "missing_core_points": [list(m) for m in missing],
                                                                   "invalid_points": [[list(k), w] for k, w in bad],
                                                                   "results": results, "bootstrap_B": B}), indent=1),
                                                  encoding="utf-8")
    plot(pts, idx, main_ev)


def plot(pts, idx, main_ev):
    fig, axes = plt.subplots(2, 2, figsize=(10, 7.5))
    for ci, qk in enumerate(QUALITY):
        for ri, dk in enumerate(DIV):
            ax = axes[ri, ci]
            for run, (name, col, mk) in ARMS.items():
                Ts = sorted(T for (r, T) in pts if r == run)
                if not Ts:
                    continue
                xs = np.array([np.nanmean(pts[(run, T)][qk]) for T in Ts])
                ys = np.array([np.nanmean(pts[(run, T)][dk]) for T in Ts])
                xe = np.array([np.percentile(np.nanmean(pts[(run, T)][qk][idx], 1), [2.5, 97.5]) for T in Ts])
                ye = np.array([np.percentile(np.nanmean(pts[(run, T)][dk][idx], 1), [2.5, 97.5]) for T in Ts])
                ax.errorbar(xs, ys, xerr=[xs - xe[:, 0], xe[:, 1] - xs], yerr=[ys - ye[:, 0], ye[:, 1] - ys],
                            color=col, marker=mk, ms=5, lw=1.2, elinewidth=0.6, capsize=1.5, label=name)
                for T, x, y in zip(Ts, xs, ys):
                    ax.annotate(f"{T:g}", (x, y), fontsize=7, xytext=(4, 3), textcoords="offset points", color=col)
            for run, lab, mk in [("sft_135m_nall_s1_v1", "SFT (T=1)", "^"), ("135m", "base (T=1)", "x"),
                                 ("teacher", "teacher (T=1)", "*")]:
                e = main_ev.get(run)
                if e:
                    x = e.get("precision_teacher_lp") if qk == "teacher_lp" else e["rougeL_sampled"]
                    if x is not None:
                        ax.scatter([x], [e[dk]], color="0.3", marker=mk, label=lab, zorder=3)
            ax.set_xlabel(QUALITY[qk], fontsize=8)
            ax.set_ylabel(DIV[dk][0], fontsize=8)
            ax.tick_params(labelsize=7)
    axes[0, 0].legend(fontsize=6)
    fig.suptitle("Dolly test, training seed 1: sample quality vs diversity across sampling temperature "
                 "(labels = T; bars = 95% prompt bootstrap)", fontsize=9)
    fig.tight_layout()
    out = ROOT / "figures"
    out.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(out / f"frontier_test.{ext}", dpi=130)
    print(f"figure -> {out / 'frontier_test.png'}")


if __name__ == "__main__":
    main()
