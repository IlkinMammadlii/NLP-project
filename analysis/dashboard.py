"""Live terminal dashboard for the grid. Reads only log/result files and nvidia-smi: no torch, no GPU use.

  .venv\\Scripts\\python.exe analysis\\dashboard.py            # simple view, refresh every 30 s (Ctrl+C quits)
  .venv\\Scripts\\python.exe analysis\\dashboard.py --detail   # every job, log lines, all metrics
  .venv\\Scripts\\python.exe analysis\\dashboard.py --once     # print once
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cachekd import protocol as P  # noqa: E402  (no torch import)
from cachekd.config import run_id  # noqa: E402

ROOT = Path(os.environ.get("CF_ROOT", Path(__file__).resolve().parent.parent / "cf_data"))
LOG = ROOT / "logs" / "grid_stdout.log"
# rough durations (minutes) until real ones are measured: train cached / online, eval of 3 sets + PopQA
EST = {"train_cache": 45, "train_online": 120, "eval": 45}
ACTIVITY = re.compile(r"^\[(\d\d:\d\d:\d\d)\] (seqkd \d+/\d+.*|cache \d+/.*|eval .*|train .*|.* step \d+/\d+ .*|"
                      r"done .*|popqa .*|FAILED.*|grid.*|generate: CUDA OOM.*)")


def jsonl(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").split("\n"):  # not splitlines(): U+2028 in text
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def jobs() -> list:
    """(label, run_id, online) for every grid job, in grid order."""
    js = [(a, d, k, s, seed, 0.0, None) for seed in P.SEEDS for a, d, k, s in P.ARMS]
    js += [(a, d, k, s, seed, P.CE_ABLATION["ce_weight"], None)
           for seed in P.CE_ABLATION["seeds"] for a, d, k, s in P.CE_ABLATION["arms"]]
    js += [(a, d, k, s, seed, 0.0, "sft") for seed in P.SFT_INIT_ABLATION["seeds"] for a, d, k, s in P.SFT_INIT_ABLATION["arms"]]
    out = []
    for a, d, k, s, seed, ce, init in js:
        rid = run_id(a, d, k, P.STUDENT, None, seed, s, P.FROZEN_LR, ce, init)
        label = f"{a}{'-k' + str(k) if k else ''}{' ' + d.upper() if d else ''}{' +CE' if ce else ''}{' sft-init' if init else ''}"
        out.append((label, rid, seed, a == "full"))
    return out


def gpu() -> str:
    try:
        q = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout.strip()
        used, total, util, temp = [x.strip() for x in q.split(",")]
        return f"{int(used) / 1024:.1f} / {int(total) / 1024:.1f} GB, {util}% busy, {temp} C"
    except Exception:
        return "nvidia-smi unavailable"


def last_activity(n: int = 6) -> list:
    if not LOG.exists():
        return ["(no grid log yet)"]
    lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]
    hits = [m.group(0)[:150] for m in map(ACTIVITY.match, lines) if m]
    return hits[-n:] or ["(no activity lines yet)"]


def fmt(v, nd=3):
    return "-" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def render() -> str:
    res = {r["run_id"]: r for r in jsonl(ROOT / "runs" / "results.jsonl")}
    evals = {}
    for r in jsonl(ROOT / "runs" / "evals.jsonl"):
        evals[(r["run_id"], r["split"])] = r  # newest row wins
    popqa = {r["run_id"]: r for r in jsonl(ROOT / "runs" / "popqa.jsonl")}
    failed = jsonl(ROOT / "runs" / "failed.jsonl")
    js = jobs()

    running = {}
    for line in last_activity(40):  # the job currently training: "<run_id> step N/M ..."
        m = re.search(r"\] (\S+) step (\d+)/(\d+)", line)
        if m:
            running[m.group(1)] = f"training {m.group(2)}/{m.group(3)}"

    def status(rid):
        if rid not in res:
            return running.get(rid, "waiting")
        n_ev = sum((rid, s) in evals for s in P.EVAL_SETS)
        if n_ev < len(P.EVAL_SETS) or rid not in popqa:
            return f"trained, eval {n_ev}/{len(P.EVAL_SETS)}{' +popqa' if rid in popqa else ''}"
        return "done"

    sts = [status(rid) for _, rid, _, _ in js]
    n_done = sum(s == "done" for s in sts)
    # time estimate from measured durations where available
    tr_c = [r["train_s"] / 60 for r in res.values() if r.get("source") != "online" and r.get("n_train", 0) > 5000]
    tr_o = [r["train_s"] / 60 for r in res.values() if r.get("source") == "online" and r.get("n_train", 0) > 5000]
    t_c = sum(tr_c) / len(tr_c) if tr_c else EST["train_cache"]
    t_o = sum(tr_o) / len(tr_o) if tr_o else EST["train_online"]
    remaining = sum((t_o if online else t_c) * (not s.startswith(("trained", "done"))) + EST["eval"] * (s != "done")
                    for (_, _, _, online), s in zip(js, sts))
    prep = []
    for split in P.EVAL_SETS:
        prep.append(f"teacher {split}: {'done' if ('teacher', split) in evals else 'pending'}")
    seqkd = ROOT / "teacher" / "seqkd_train.jsonl"
    n_seqkd = sum(1 for _ in open(seqkd, encoding="utf-8")) if seqkd.exists() else 0

    W = 100
    out = []
    now = datetime.now()
    out.append("=" * W)
    out.append(f" WHAT THE CACHE FORGETS - grid dashboard        {now:%a %d %b %H:%M:%S}   (refresh 30 s, Ctrl+C quits)")
    out.append("=" * W)
    out.append(f" GPU:  {gpu()}")
    out.append(f" Jobs: {n_done} / {len(js)} done   |   remaining ~{remaining / 60:.1f} h   "
               f"|   est. finish {(now + timedelta(minutes=remaining)):%a %d %b %H:%M}")
    bar = int(40 * n_done / len(js))
    out.append(f"       [{'#' * bar}{'.' * (40 - bar)}]")
    out.append(f" Prep: cache {'done' if (ROOT / 'cache' / 'train' / 'meta.json').exists() else 'pending'}   "
               f"|   seq-KD answers {n_seqkd}/11221   |   " + "   ".join(prep))
    open_fail = [f for f in failed if not (f.get("run_id") and (f["run_id"], f.get("split")) in evals)
                 and f.get("stage") not in ("cache", "seqkd")]
    out.append(f" Failures logged: {len(failed)} ({len(open_fail)} unresolved; early cache/seq-KD ones were fixed)")
    out.append("-" * W)
    out.append(" NOW (latest log lines):")
    for line in last_activity():
        out.append("   " + line)
    out.append("-" * W)
    out.append(f" {'job':28s} {'seed':>4s}  {'status':28s} {'ROUGE-L':>7s} {'sem.div':>7s} {'dist2':>6s} "
               f"{'ent.ratio':>9s} {'ECE':>6s} {'PopQA':>6s}")
    t = evals.get(("teacher", "test"), {})
    out.append(f" {'teacher (reference)':28s} {'':>4s}  {'':28s} {fmt(t.get('rougeL_greedy'), 1):>7s} "
               f"{fmt(t.get('semantic_div')):>7s} {fmt(t.get('distinct2_first32')):>6s} {'1.000':>9s} {'-':>6s} {'1.000':>6s}")
    b = popqa.get(P.STUDENT, {})
    out.append(f" {'base ' + P.STUDENT + ' (untrained)':28s} {'':>4s}  {'':28s} {'-':>7s} {'-':>7s} {'-':>6s} "
               f"{'-':>9s} {'-':>6s} {fmt(b.get('acc')):>6s}")
    for (label, rid, seed, _), s in zip(js, sts):
        if s == "waiting" and seed != P.SEEDS[0]:
            continue  # keep the screen short: later seeds appear once they start
        e = evals.get((rid, "test"), {})
        pq = popqa.get(rid, {})
        out.append(f" {label:28s} {seed:>4d}  {s[:28]:28s} {fmt(e.get('rougeL_greedy'), 1):>7s} "
                   f"{fmt(e.get('semantic_div')):>7s} {fmt(e.get('distinct2_first32')):>6s} "
                   f"{fmt(e.get('tok_entropy_ratio')):>9s} {fmt(e.get('tok_ece')):>6s} {fmt(pq.get('acc')):>6s}")
    hidden = sum(1 for (_, _, seed, _), s in zip(js, sts) if s == "waiting" and seed != P.SEEDS[0])
    if hidden:
        out.append(f" ... {hidden} later jobs (seeds 2-3, ablations) not started yet")
    out.append("-" * W)
    out.append(" Test-set metrics (Dolly test, 500 prompts). sem.div / dist2: higher = more diverse; ent.ratio: student/"
               "teacher entropy; ECE: lower = better calibrated; PopQA: accuracy on teacher-correct questions.")
    return "\n".join(out)



FRIENDLY = {"sft": "SFT (no teacher, baseline)", "seqkd": "Seq-KD (teacher's written answers)",
            "topk": "Top-{k} cache", "tailbucket": "Top-{k} cache + tail bucket",
            "rs": "Random-sampling cache", "full": "Full teacher (no cache)"}


def friendly(label: str) -> str:
    m = re.match(r"(\w+?)(?:-k(\d+))?(?: (FKL|RKL))?( \+CE)?( sft-init)?$", label)
    arm, k, div, ce, init = m.groups()
    name = FRIENDLY[arm].format(k=k)
    if div:
        name += ", forward KL" if div == "FKL" else ", reverse KL"
    if ce:
        name += ", + CE ablation"
    if init:
        name += ", SFT-init ablation"
    return name


def gpu_simple(teacher_in_memory: bool = False) -> str:
    """OK / CHECK on temperature. Memory near 8 GB is expected while the 1.7B teacher sits on the GPU
    (full-KD training, testing): nvidia-smi also counts PyTorch's reserved cache, so it reads ~7.8 GB."""
    try:
        q = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,temperature.gpu", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10).stdout.strip()
        used, temp = [int(x) for x in q.split(",")]
    except Exception:
        return "unknown (nvidia-smi not available)"
    hot = temp >= 85
    full = used >= 7900 and not teacher_in_memory
    note = ""
    if hot:
        note = "   <- hot: check the cooling"
    elif full:
        note = "   <- memory nearly full"
    elif teacher_in_memory and used >= 7000:
        note = "   (high memory is normal while the teacher is loaded)"
    return f"{'CHECK' if hot or full else 'OK'}   ({temp} C, {used / 1024:.1f} of 8 GB memory){note}"


def render_simple() -> str:
    res = {r["run_id"]: r for r in jsonl(ROOT / "runs" / "results.jsonl")}
    evals = {}
    for r in jsonl(ROOT / "runs" / "evals.jsonl"):
        evals[(r["run_id"], r["split"])] = r
    popqa = {r["run_id"]: r for r in jsonl(ROOT / "runs" / "popqa.jsonl")}
    failed = jsonl(ROOT / "runs" / "failed.jsonl")
    js = jobs()
    done = [rid in res and all((rid, sp) in evals for sp in P.EVAL_SETS) and rid in popqa for _, rid, _, _ in js]
    n_done = sum(done)

    # right now: the first unfinished job
    steps = {}
    for line in last_activity(60):
        m = re.search(r"\] (\S+) step (\d+)/(\d+)", line)
        if m:
            steps[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    now_txt = "Preparing (teacher answers and teacher tests)"
    teacher_busy = True  # the teacher is on the GPU during preparation
    if (ROOT / "cache" / "train" / "meta.json").exists() and all(("teacher", sp) in evals for sp in P.EVAL_SETS):
        now_txt = "All jobs finished!" if n_done == len(js) else ""
        teacher_busy = False
        for i, ((label, rid, seed, online), d) in enumerate(zip(js, done)):
            if d:
                continue
            teacher_busy = rid in res or online  # testing loads the teacher too (token metrics)
            who = f"student {i + 1} of {len(js)}: {friendly(label)}, seed {seed}"
            if rid in res:
                n_ev = sum((rid, sp) in evals for sp in P.EVAL_SETS) + (rid in popqa)
                now_txt = f"Testing {who}  (test {n_ev + 1} of {len(P.EVAL_SETS) + 1})"
            elif rid in steps:
                a, b = steps[rid]
                now_txt = f"Training {who}  ({100 * a // b}% done)"
            else:
                now_txt = f"Starting {who}"
            break

    # finish estimate (same rule as the detailed view)
    tr_c = [r["train_s"] / 60 for r in res.values() if r.get("source") != "online" and r.get("n_train", 0) > 5000]
    tr_o = [r["train_s"] / 60 for r in res.values() if r.get("source") == "online" and r.get("n_train", 0) > 5000]
    t_c = sum(tr_c) / len(tr_c) if tr_c else EST["train_cache"]
    t_o = sum(tr_o) / len(tr_o) if tr_o else EST["train_online"]
    left = sum(((t_o if on else t_c) if rid not in res else 0) + (EST["eval"] if not d else 0)
               for (_, rid, _, on), d in zip(js, done))
    now = datetime.now()
    finish = now + timedelta(minutes=left)

    bar = int(30 * n_done / len(js))
    open_fail = [f for f in failed if f.get("stage") not in ("cache", "seqkd")
                 and not (f.get("run_id") and (f["run_id"], f.get("split")) in evals)]
    L = []
    L.append(f"  WHAT THE CACHE FORGETS - progress                {now:%a %d %b, %H:%M}")
    L.append("  " + "-" * 66)
    L.append(f"  Overall:      [{'#' * bar}{'.' * (30 - bar)}]  {n_done} of {len(js)} students done")
    L.append(f"  Finishes:     {finish:%A %d %b, around %H:%M}   (about {left / 60:.0f} hours left)")
    L.append("")
    from cachekd import sweepplan
    plan_pts = sweepplan.points()
    main_ids = {run_id(a_, d_, k_, P.STUDENT, None, s_, src_, P.FROZEN_LR) for s_ in P.SEEDS for a_, d_, k_, src_ in P.ARMS}
    main_done = all(d for (_, rid, _, _), d in zip(js, done) if rid in main_ids)
    sweep_done = {(r["run_id"], r["temperature"]) for r in jsonl(ROOT / "runs" / "tempsweep.jsonl")}
    n_t = sum((rid, T) in sweep_done for rid, T in plan_pts)
    if main_done and n_t < len(plan_pts):
        now_txt = f"Temperature sweep ({n_t} of {len(plan_pts)} points done)"
        teacher_busy = True  # each point loads the teacher to score the samples
    L.append(f"  Right now:    {now_txt}")
    L.append(f"  GPU:          {gpu_simple(teacher_busy)}")
    L.append(f"  Problems:     {'none' if not open_fail else str(len(open_fail)) + ' failed step(s)'}")
    L.append("")
    fin = [(label, rid, seed) for (label, rid, seed, _), d in zip(js, done) if d]
    if not fin:
        L.append("  Results:      none yet (the first student's results appear here when it is tested)")
    else:
        t = evals.get(("teacher", "test"), {})
        L.append(f"  Results on the Dolly test set:              quality   variety   calibration error")
        L.append(f"                                              (higher)  (higher)  (lower = better)")
        L.append(f"    {'Teacher (reference)':40s}  {fmt(t.get('rougeL_greedy'), 1):>7s}   {fmt(t.get('semantic_div'), 2):>7s}        -")
        for label, rid, seed in fin[-12:]:
            e = evals.get((rid, "test"), {})
            tag = f", seed {seed}"
            L.append(f"    {friendly(label)[:40 - len(tag)] + tag:40s}  {fmt(e.get('rougeL_greedy'), 1):>7s}   "
                     f"{fmt(e.get('semantic_div'), 2):>7s}   {fmt(e.get('tok_ece'), 3):>9s}")
        if len(fin) > 12:
            L.append(f"    ... and {len(fin) - 12} earlier ones (see --detail)")
    L.append("  " + "-" * 66)
    L.append("  Updates every 30 s.  More detail: add --detail.  Close: Ctrl+C (the experiment keeps running).")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--every", type=int, default=30)
    ap.add_argument("--detail", action="store_true", help="every job, latest log lines, more metrics")
    a = ap.parse_args()
    view = render if a.detail else render_simple
    if a.once:
        print(view())
        return
    os.system("")  # enables ANSI escapes in the Windows console
    while True:
        try:
            screen = view()
        except Exception as e:  # never crash the dashboard on a half-written file
            screen = f"dashboard error (will retry): {e!r}"
        sys.stdout.write("\033[2J\033[H" + screen + "\n")
        sys.stdout.flush()
        time.sleep(a.every)


if __name__ == "__main__":
    main()
