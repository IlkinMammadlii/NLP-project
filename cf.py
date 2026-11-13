"""What the Cache Forgets: command line. Every phase is resumable; rerun after any stop.

  python cf.py data                      # Dolly splits (MiniLLM protocol)
  python cf.py pilot                     # Day 1: SFT, full KD, top-8 (FKL+RKL) on 2k examples
  python cf.py sweep                     # LR {1e-4, 2e-4, 3e-4} on SFT and full-KD FKL, n=2k, valid ROUGE-L
  python cf.py cache                     # Day 2: teacher pass -> top-128 probs, ids, tail mass, RS draws
  python cf.py seqkd                     # teacher greedy responses for the seq-KD arm
  python cf.py grid                      # the frozen main grid (cachekd/protocol.py), train + eval
  python cf.py tempsweep                 # sampling-temperature sweep (cachekd/sweepplan.py); also runs inside grid
  python cf.py train --arm topk --k 8 --div fkl --seed 1 [--lr 3e-4] [--ce-weight 0.5]
  python cf.py eval --run <run_id> --split valid|test|selfinst|vicuna|sni
  python cf.py popqa-teacher             # teacher answers on PopQA (defines the teacher-correct probe set)
  python cf.py popqa --run <run_id>      # student accuracy by popularity bucket
  python cf.py table                     # mean +- std over seeds of every metric
  python cf.py status
"""
import argparse
import re

from cachekd.utils import log, path, read_jsonl

PILOT_N, PILOT_EVAL_N = 2000, 200
PILOT_ARMS = [("sft", None, None), ("full", "fkl", None), ("topk", "fkl", 8),
              ("full", "rkl", None), ("topk", "rkl", 8)]
SWEEP_LRS = [1e-4, 2e-4, 3e-4]  # 2e-4 runs are the pilot's (same id), reused
SWEEP_ARMS = [("sft", None), ("full", "fkl")]
EVAL_SETS = ["valid", "test", "selfinst", "vicuna", "sni"]


def cmd_data(a):
    from cachekd.data import build_splits
    from cachekd.models import load_tokenizer
    build_splits(load_tokenizer("135m"))


def cmd_train(a):
    from cachekd.train import train
    train(a.arm, a.div, a.k, a.student, a.n, a.seed, a.source, lr=a.lr, ce_weight=a.ce_weight)


def cmd_eval(a):
    from cachekd.evaluate import evaluate
    evaluate(a.run, a.split, a.n_prompts)


def cmd_cache(a):
    from cachekd.teacher import build_cache
    build_cache("train", a.n)


def cmd_seqkd(a):
    from cachekd.teacher import build_seqkd
    build_seqkd(a.n)


def cmd_popqa_teacher(a):
    from cachekd.popqa import teacher_pass
    teacher_pass(a.n)


def cmd_popqa(a):
    from cachekd.popqa import evaluate_popqa
    evaluate_popqa(a.run)


def cmd_pilot(a):
    """Top-k vs full KD on 2k examples; teacher online for every arm (no cache needed yet)."""
    from cachekd.evaluate import evaluate
    from cachekd.train import train
    evaluate("teacher", "valid", PILOT_EVAL_N)
    for seed in a.seeds:
        for arm, div, k in PILOT_ARMS:
            rid = train(arm, div, k, "135m", PILOT_N, seed, source="online")
            evaluate(rid, "valid", PILOT_EVAL_N)
    a.filter = f"n{PILOT_N}_"
    cmd_table(a)


def cmd_sweep(a):
    """Dev LR choice (authors, 17 Oct): one shared LR, picked by mean validation ROUGE-L (greedy) of SFT
    and full-KD FKL at n=2k, seed 1. Only the valid split is used."""
    import numpy as np
    from cachekd.evaluate import evaluate
    from cachekd.train import train
    evaluate("teacher", "valid", PILOT_EVAL_N)
    rids = {}
    for lr in SWEEP_LRS:
        for arm, div in SWEEP_ARMS:
            rids[(lr, arm)] = train(arm, div, None, "135m", PILOT_N, 1, source="online", lr=lr, ce_weight=0.0)
            evaluate(rids[(lr, arm)], "valid", PILOT_EVAL_N)
    ev = {r["run_id"]: r for r in read_jsonl(path("runs", "evals.jsonl"))  # last row wins (newest schema)
          if r["split"] == "valid" and r["n_prompts"] == PILOT_EVAL_N}
    scores = {}
    for lr in SWEEP_LRS:
        vals = [ev[rids[(lr, arm)]]["rougeL_greedy"] for arm, div in SWEEP_ARMS]
        scores[lr] = float(np.mean(vals))
        log(f"lr {lr:.0e}: " + ", ".join(f"{arm}{'-' + div if div else ''} {v:.2f}"
                                          for (arm, div), v in zip(SWEEP_ARMS, vals)) + f" | mean {scores[lr]:.2f}")
    best = max(scores, key=scores.get)
    ranked = sorted(scores, key=scores.get, reverse=True)
    log(f"best LR by mean validation ROUGE-L: {best:g} (margin over 2nd: {scores[ranked[0]] - scores[ranked[1]]:.2f} "
        f"ROUGE-L, one seed, 200 prompts). Freeze it in cachekd/protocol.py (FROZEN_LR) and EXPERIMENT_LOG.md.")


def cmd_grid(a):
    """The frozen main grid (cachekd/protocol.py), seed-major so seed 1 of every arm finishes first:
    teacher cache + seq-KD responses, teacher evals, then per job train -> eval sets -> PopQA.
    --with-ablation adds the KD+CE ablation; --sft-init adds the MiniLLM-style-init ablation after the
    main grid. Every failure is logged (runs/failed.jsonl) and the grid moves on; rerun to resume."""
    from cachekd import protocol as P
    from cachekd.config import run_id
    from cachekd.evaluate import evaluate
    from cachekd.popqa import evaluate_popqa
    from cachekd.teacher import build_cache, build_seqkd
    from cachekd.train import train
    from cachekd.utils import append_jsonl
    if P.FROZEN_LR is None:
        raise SystemExit("freeze the LR first (cf.py sweep -> cachekd/protocol.py, EXPERIMENT_LOG.md)")

    def failed(what: dict, e: Exception) -> None:  # report every failure, including later-resolved ones
        append_jsonl(path("runs", "failed.jsonl"), {**what, "error": repr(e)})
        log(f"FAILED {what}: {e!r}")

    def attempt(what: dict, fn, *args, **kw):
        import gc
        import torch
        try:
            return fn(*args, **kw)
        except Exception as e:
            failed(what, e)
            return None
        finally:  # a failed stage must not leave its model on the GPU for the next one
            gc.collect()
            torch.cuda.empty_cache()

    # (arm, div, k, source, seed, ce_weight, init_from); "TEMPSWEEP" marks where the sampling-temperature
    # sweep runs: after the main grid, before the ablations (authors, 19 Oct)
    jobs = [(arm, div, k, src, s, 0.0, None) for s in P.SEEDS for arm, div, k, src in P.ARMS]
    if not a.main_only:  # --main-only: the main grid alone (run_final.py owns everything after it)
        jobs.append("TEMPSWEEP")
    if a.with_ablation and not a.main_only:
        jobs += [(arm, div, k, src, s, P.CE_ABLATION["ce_weight"], None)
                 for s in P.CE_ABLATION["seeds"] for arm, div, k, src in P.CE_ABLATION["arms"]]
    if a.sft_init and not a.main_only:
        jobs += [(arm, div, k, src, s, 0.0, run_id("sft", None, None, P.STUDENT, None, s, lr=P.FROZEN_LR))
                 for s in P.SFT_INIT_ABLATION["seeds"] for arm, div, k, src in P.SFT_INIT_ABLATION["arms"]]
    log(f"grid: {len(jobs)} entries, LR {P.FROZEN_LR:g}, eval sets {P.EVAL_SETS} + PopQA")
    attempt({"stage": "cache"}, build_cache, "train")
    attempt({"stage": "seqkd"}, build_seqkd)
    for split in P.EVAL_SETS:  # teacher samples first: every student's recall metric needs them
        attempt({"run_id": "teacher", "split": split, "stage": "eval"}, evaluate, "teacher", split)
    attempt({"run_id": P.STUDENT, "stage": "popqa"}, evaluate_popqa, P.STUDENT)  # untrained base: retention reference
    stop_f = path("STOP")
    for job in jobs:
        if stop_f.exists():  # clean pause between jobs: create $CF_ROOT/STOP, rerun cf.py grid to continue
            stop_f.unlink()
            log("STOP file found: grid paused between jobs (rerun `cf.py grid ...` to continue)")
            return
        if job == "TEMPSWEEP":
            from cachekd.tempsweep import run_plan  # registered plan: cachekd/sweepplan.py
            attempt({"stage": "tempsweep"}, run_plan)
            continue
        arm, div, k, src, seed, ce_w, init = job
        rid = attempt({"job": [arm, div, k, src, seed, ce_w, init], "stage": "train"}, train, arm, div, k,
                      P.STUDENT, None, seed, src, lr=P.FROZEN_LR, ce_weight=ce_w, init_from=init)
        if rid is None:
            continue
        for split in P.EVAL_SETS:  # one failed eval set does not skip the others
            attempt({"run_id": rid, "split": split, "stage": "eval"}, evaluate, rid, split)
        attempt({"run_id": rid, "stage": "popqa"}, evaluate_popqa, rid)
    log("grid pass finished; `cf.py status` lists unresolved failures (rerun `cf.py grid` to retry them)")


def cmd_job(a):
    """One grid job outside the grid (run_final.py): train (resumable) -> eval sets -> PopQA, exactly as
    `cf.py grid` does per job. --sft-init starts from the same-seed SFT student of the main grid."""
    from cachekd import protocol as P
    from cachekd.config import run_id
    from cachekd.evaluate import evaluate
    from cachekd.popqa import evaluate_popqa
    from cachekd.train import train
    if a.arm in ("topk", "tailbucket", "tailuniform") and not (a.k and a.k > 0):
        raise SystemExit("--k > 0 is required for topk / tailbucket / tailuniform")
    if a.arm in ("rs", "tailuniform") and a.div != "fkl":
        raise SystemExit(f"{a.arm} runs with FKL only")
    init = run_id("sft", None, None, P.STUDENT, None, a.seed, lr=P.FROZEN_LR) if a.sft_init else None
    src = "online" if a.arm == "full" else "cache"
    rid = train(a.arm, a.div, a.k, P.STUDENT, None, a.seed, src, lr=P.FROZEN_LR, ce_weight=a.ce_weight,
                init_from=init)
    bad = 0
    for split in P.EVAL_SETS + (["popqa"] if not a.no_popqa else []):
        try:
            evaluate_popqa(rid) if split == "popqa" else evaluate(rid, split)
        except Exception as e:  # noqa: BLE001  one failed eval set does not skip the others
            from cachekd.utils import append_jsonl
            append_jsonl(path("runs", "failed.jsonl"), {"run_id": rid, "split": split, "stage": "eval", "error": repr(e)})
            log(f"FAILED eval {rid} {split}: {e!r}")
            bad += 1
    raise SystemExit(1 if bad else 0)


def cmd_point(a):
    """One sampling point (cachekd/tempsweep.sample_point): --run, --split, --T, --n prompts."""
    from cachekd.tempsweep import sample_point
    sample_point(a.run, a.split, a.T, n=a.n)


def cmd_tempsweep(a):
    """Sampling-temperature sweep on finished seed-1 checkpoints (plan: cachekd/sweepplan.py)."""
    from cachekd.tempsweep import run_plan
    run_plan()


def cmd_table(a):
    import numpy as np
    from cachekd.metrics import summarize_seeds
    latest = {}  # one row per (run, split, n, seed, version): the newest eval schema wins
    for r in read_jsonl(path("runs", "evals.jsonl")):
        latest[(r["run_id"], r["split"], r["n_prompts"], r["seed"], r.get("config_version"))] = r
    rows = [r for r in latest.values()
            if (not a.filter or a.filter in r["run_id"] or r["run_id"] == "teacher") and r["split"] == a.split]
    groups = {}
    for r in rows:
        key = re.sub(r"_s\d+(_v\w+)$", r"\1", r["run_id"])  # drop the training seed, keep the version
        groups.setdefault((key, r["n_prompts"], r.get("config_version")), []).append(r)
    cols = ["rougeL_greedy", "rougeL_sampled", "distinct2", "distinct2_first32", "len_sampled", "self_bleu",
            "semantic_div", "tok_H_q", "tok_entropy_ratio", "tok_q_out_top8", "tok_tail_cov_top8",
            "tok_tail_cond_kl_top8", "tok_goldtail8_nll_s", "tok_freqb0_nll_s", "tok_fkl", "tok_ece",
            "precision_teacher_lp", "recall_student_lp"]
    print(f"{'run':48s} n_seeds " + " ".join(f"{c[:13]:>15s}" for c in cols))
    for (key, n, ver), rs in sorted(groups.items(), key=str):
        cells = []
        for c in cols:
            m, s, k = summarize_seeds([r.get(c, float("nan")) for r in rs])
            cells.append("—" if k == 0 else (f"{m:.3f}" if np.isnan(s) else f"{m:.3f}±{s:.3f}"))
        print(f"{key:48s} {len(rs):7d} " + " ".join(f"{c:>15s}" for c in cells))


def cmd_status(a):
    res = read_jsonl(path("runs", "results.jsonl"))
    ev = read_jsonl(path("runs", "evals.jsonl"))
    failed = read_jsonl(path("runs", "failed.jsonl"))
    done_ev = {(r["run_id"], r["split"]) for r in ev}
    open_f = [f for f in failed if ("split" in f and (f["run_id"], f["split"]) not in done_ev) or "job" in f]
    log(f"{len(res)} trained runs, {len(ev)} evals, {len(failed)} failures logged "
        f"({len(open_f)} not yet resolved by a later success); root {path('x').parent}")
    for f in open_f[-5:]:
        print(f"  unresolved: {f}")
    for r in res[-10:]:
        print(f"  {r['run_id']}: {r['steps']} steps, {r['train_s']}s, peak {r['peak_gpu_mem_gb']} GB")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("data").set_defaults(f=cmd_data)
    p = sp.add_parser("train")
    p.add_argument("--arm", required=True, choices=["sft", "seqkd", "full", "topk", "tailbucket", "rs", "tailuniform"])
    p.add_argument("--div", choices=["fkl", "rkl"])
    p.add_argument("--k", type=int)
    p.add_argument("--student", default="135m", choices=["135m", "360m"])
    p.add_argument("--n", type=int, help="first n training examples (default: all)")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--source", default="cache", choices=["cache", "online"])
    p.add_argument("--lr", type=float, help="default: CFG['lr']; non-default LRs are tagged in the run id")
    p.add_argument("--ce-weight", type=float, help="extra CE on gold tokens for KD arms (ablation)")
    p.set_defaults(f=cmd_train)
    p = sp.add_parser("eval")
    p.add_argument("--run", required=True)
    p.add_argument("--split", default="valid", choices=EVAL_SETS)
    p.add_argument("--n-prompts", type=int)
    p.set_defaults(f=cmd_eval)
    for name, f in [("cache", cmd_cache), ("seqkd", cmd_seqkd), ("popqa-teacher", cmd_popqa_teacher)]:
        p = sp.add_parser(name)
        p.add_argument("--n", type=int)
        p.set_defaults(f=f)
    p = sp.add_parser("popqa")
    p.add_argument("--run", required=True)
    p.set_defaults(f=cmd_popqa)
    p = sp.add_parser("pilot")
    p.add_argument("--seeds", type=int, nargs="+", default=[1])
    p.add_argument("--split", default="valid")
    p.set_defaults(f=cmd_pilot)
    sp.add_parser("sweep").set_defaults(f=cmd_sweep)
    p = sp.add_parser("grid")
    p.add_argument("--with-ablation", action="store_true", help="also the KD+CE (0.5) ablation")
    p.add_argument("--sft-init", action="store_true", help="also the MiniLLM-style (SFT) init ablation, after the main grid")
    p.add_argument("--main-only", action="store_true", help="only the main grid (no sweep, no ablations)")
    p.set_defaults(f=cmd_grid)
    p = sp.add_parser("job")
    p.add_argument("--arm", required=True, choices=["full", "topk", "tailbucket", "rs", "tailuniform"])
    p.add_argument("--div", required=True, choices=["fkl", "rkl"])
    p.add_argument("--k", type=int)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--ce-weight", type=float, default=0.0)
    p.add_argument("--sft-init", action="store_true")
    p.add_argument("--no-popqa", action="store_true")
    p.set_defaults(f=cmd_job)
    p = sp.add_parser("point")
    p.add_argument("--run", required=True)
    p.add_argument("--split", required=True, choices=["valid", "test"])
    p.add_argument("--T", type=float, required=True)
    p.add_argument("--n", type=int)
    p.set_defaults(f=cmd_point)
    sp.add_parser("tempsweep").set_defaults(f=cmd_tempsweep)
    p = sp.add_parser("table")
    p.add_argument("--filter", default="")
    p.add_argument("--split", default="valid", choices=EVAL_SETS)
    p.set_defaults(f=cmd_table)
    sp.add_parser("status").set_defaults(f=cmd_status)
    a = ap.parse_args()
    if a.cmd == "train" and a.arm not in ("sft", "seqkd") and not a.div:
        ap.error("--div is required for KD arms")
    if a.cmd == "train" and a.arm in ("topk", "tailbucket", "tailuniform") and not a.k:
        ap.error("--k is required for topk / tailbucket")
    if a.cmd == "train" and a.arm == "rs" and a.div == "rkl":
        ap.error("RS-KD runs with FKL only (authors, 17 Oct): the support-restricted RKL is biased")
    a.f(a)


if __name__ == "__main__":
    main()
