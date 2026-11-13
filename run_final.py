"""The autonomous final queue (authors' final instructions, 19 Oct 2026). Launch ONCE from the repo root:

    .venv\\Scripts\\python.exe run_final.py

It needs no further help: every step is resumable and skips finished work, so after any stop (crash, reboot,
closed lid) just launch it again. Order (pre-registered in EXPERIMENT_LOG.md, 19 Oct 14:05):

  a) the main grid's seed 3: waits for the running `cf.py grid` process; when its last main job has started it
     places cf_data/STOP, which that process reads only BETWEEN jobs, so it ends cleanly after the main grid.
     If no grid process is alive and main jobs are missing, it runs `cf.py grid --main-only` itself.
  b) temperature selection on valid (seed 1, first 300 prompts): top-8 at {1.0, 1.15, 1.3, 1.5}, full KD at
     {0.7, 0.85, 1.0}; coded rule in `select_T` (one extra step of 0.15 if the target is not bracketed).
  c) matched test points, training seeds 1 and 2: A = full@1.0 vs top-8@T_top8*, B = top-8@1.0 vs full@T_full*.
  d) untrained base student on Dolly test (the teacher's T = 1.0 evaluation already exists).
     + the CPU/GPU analyses of the old after-grid waiter (calibration, bootstrap, length control).
  e) ablations: KD+CE 0.5 and SFT-init FKL (top-8, full) seed 1, then seed 2; then the rest of the SFT-init
     2x4 (seed 1, then seed 2).
  f) top-8 + tail mass spread uniformly (allocation diagnostic), seed 1.
Every GPU step is a subprocess (crash-isolated; code is read fresh). make_results.py runs after every stage.
The whole queue is walked twice: the second pass retries anything that failed in the first.
Progress: STATUS.md (auto block), EXPERIMENT_LOG.md (stage events), cf_data/logs/run_final.log.
"""
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
os.chdir(REPO)

from cachekd import protocol as P  # noqa: E402
from cachekd.config import CFG, CONFIG_VERSION, run_id  # noqa: E402
from cachekd.utils import path, read_jsonl  # noqa: E402

PY = sys.executable
SEL_N = 300
SEL_TOP8 = (1.0, 1.15, 1.3, 1.5)
SEL_FULL = (0.7, 0.85, 1.0)
STEP = 0.15
TEST_SEEDS = (1, 2)
LOG = path("logs", "run_final.log")
JOBLOG = path("logs", "run_final_jobs.log")
STATE = path("runs", "run_final_state.json")
SELECTION = path("runs", "final_selection.json")


def top8(seed: int) -> str:
    return run_id("topk", "fkl", 8, P.STUDENT, None, seed, "cache")


def full(seed: int) -> str:
    return run_id("full", "fkl", None, P.STUDENT, None, seed, "online")


# ------------------------------------------------------------------ logging / status
def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def say(msg: str, explog: bool = False) -> None:
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if explog:
        with open(REPO / "EXPERIMENT_LOG.md", "a", encoding="utf-8") as f:
            f.write(f"- {now()} run_final: {msg}\n")


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"done": [], "failed": [], "running": None}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    for attempt in range(10):  # Windows: a reader (editor, indexer, antivirus) can hold the file for a moment
        try:
            os.replace(tmp, STATE)
            break
        except PermissionError:
            time.sleep(1 + attempt)
    try:
        write_status(st)
    except OSError as e:  # STATUS.md is informational: never stop the queue for it
        print(f"could not write STATUS.md: {e!r}", flush=True)


AUTO_BEGIN, AUTO_END = "<!-- AUTO-STATUS BEGIN (written by run_final.py) -->", "<!-- AUTO-STATUS END -->"


def write_status(st: dict) -> None:
    f = REPO / "STATUS.md"
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        text = "# STATUS\n\n" + AUTO_BEGIN + "\n" + AUTO_END + "\n"
    if AUTO_BEGIN not in text:
        text += "\n" + AUTO_BEGIN + "\n" + AUTO_END + "\n"
    head, rest = text.split(AUTO_BEGIN, 1)
    tail = rest.split(AUTO_END, 1)[1] if AUTO_END in rest else ""
    try:
        sel = json.loads(SELECTION.read_text(encoding="utf-8")) if SELECTION.exists() else None
    except (OSError, ValueError):
        sel = "unreadable"
    done = st["done"]
    lines = [f"Updated {now()} by run_final.py (pid {os.getpid()}).", "",
             f"**Running:** {st.get('running') or 'nothing (queue idle or finished)'}", "",
             f"**Done steps ({len(done)}):** " + ("; ".join(done[-40:]) if done else "none yet"), "",
             f"**Failed steps ({len(st['failed'])}):** " + ("; ".join(st["failed"][-20:]) if st["failed"] else "none"),
             "", "**Next:** " + "; ".join(n for n in st.get("next", [])[:12]) if st.get("next") else "**Next:** -",
             "", f"**Temperature selection:** {json.dumps(sel) if sel else 'not yet made'}"]
    f.write_text(head + AUTO_BEGIN + "\n" + "\n".join(lines) + "\n" + AUTO_END + tail, encoding="utf-8")


# ------------------------------------------------------------------ process helpers
def keep_awake() -> None:
    """Ask Windows not to idle-sleep while this process runs (it cannot stop a closed lid)."""
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # CONTINUOUS | SYSTEM_REQUIRED


def _script_procs(script: str) -> list:
    """[(pid, create_time, argv after the script)] of python processes running THIS repo's `script`
    (relative paths are resolved against the process's working directory)."""
    import psutil
    me, parent = os.getpid(), os.getppid()
    target = (REPO / script).resolve()
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "cwd", "create_time"]):
        try:
            if p.info["pid"] in (me, parent) or not (p.info["name"] or "").lower().startswith("python"):
                continue
            argv = p.info["cmdline"] or []
            for i, a in enumerate(argv[1:], start=1):
                if a.replace("\\", "/").endswith(script):
                    f = Path(a) if Path(a).is_absolute() else Path(p.info["cwd"] or REPO) / a
                    if f.resolve() == target:
                        out.append((p.info["pid"], p.info["create_time"], argv[i + 1:]))
                    break
        except Exception:  # noqa: BLE001  (process vanished / access denied)
            continue
    return out


def grid_procs() -> list:
    """Running `cf.py grid` processes of this repo (any flags, including an orphaned --main-only)."""
    return [p for p in _script_procs("cf.py") if p[2][:1] == ["grid"]]


def grid_alive() -> bool:
    return bool(grid_procs())


def wait_gpu_free() -> None:
    """Never start a step while another cf.py process of this repo runs (the grid, or an orphaned child of an
    earlier queue process): the GPU has 8 GB. Waits; never kills anything."""
    first = True
    while _script_procs("cf.py"):
        if first:
            say("another cf.py process is running; waiting for it before the next step")
            first = False
        time.sleep(60)


def run(step: str, args: list, st: dict) -> bool:
    """One subprocess step; True on exit code 0. Output appended to cf_data/logs/run_final_jobs.log."""
    wait_gpu_free()
    st["running"] = f"{step} (since {now()})"
    save_state(st)
    say(f"start {step}: {' '.join(args)}")
    t0 = time.time()
    try:
        with open(JOBLOG, "a", encoding="utf-8") as out:
            out.write(f"\n===== [{now()}] {step}: {' '.join(args)}\n")
            out.flush()
            rc = subprocess.call([PY, *args], cwd=REPO, stdout=out, stderr=subprocess.STDOUT,
                                 env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    except Exception as e:  # noqa: BLE001  (launch / file-system error): a failed step, not a dead queue
        say(f"could not run {step}: {e!r}")
        rc = -1
    mins = (time.time() - t0) / 60
    st["running"] = None
    if rc == 0:
        if step not in st["done"]:
            st["done"].append(step)
        st["failed"] = [x for x in st["failed"] if not x.startswith(step + " ")]
        say(f"done {step} ({mins:.0f} min)")
    else:
        st["failed"].append(f"{step} (exit {rc}, {now()})")
        say(f"FAILED {step}: exit {rc} after {mins:.0f} min (see cf_data/logs/run_final_jobs.log)", explog=True)
    save_state(st)
    return rc == 0


def fail(st: dict, what: str) -> None:
    st["failed"].append(f"{what} ({now()})")
    save_state(st)
    say(f"FAILED {what}", explog=True)


def results(st: dict) -> None:
    if (REPO / "make_results.py").exists():
        run("make_results", ["make_results.py"], st)


# ------------------------------------------------------------------ (a) main grid
def main_rids() -> list:
    return [run_id(arm, div, k, P.STUDENT, None, s, src) for s in P.SEEDS for arm, div, k, src in P.ARMS]


def stage_a(st: dict) -> None:
    from cachekd.train import done_runs
    arm, div, k, src = P.ARMS[-1]
    last = run_id(arm, div, k, P.STUDENT, None, P.SEEDS[-1], src)
    last_log = path("logs", f"{last}.log")
    while True:
        procs = grid_procs()
        if procs:
            # the last main job has started IN THIS grid process: its log was written after the process started
            started = (last in done_runs()) or (last_log.exists() and last_log.stat().st_mtime > min(p[1] for p in procs))
            ident = ",".join(f"{p[0]}@{p[1]:.0f}" for p in procs)
            if started and st.get("stop_placed_for") != ident:
                path("STOP").write_text("placed by run_final.py: end the grid after the main grid\n")
                st["stop_placed_for"] = ident
                save_state(st)
                say(f"last main job {last} has started: placed cf_data/STOP so the running grid ends after it "
                    "(it reads STOP only between jobs)", explog=True)
            st["running"] = "main grid (the original `cf.py grid` process)"
            save_state(st)
            time.sleep(120)
            continue
        if path("STOP").exists():  # grid is gone: a leftover STOP would pause our own grid run
            path("STOP").unlink()
        missing = [r for r in main_rids() if r not in done_runs()]
        say(f"no grid process alive; {len(missing)} main runs not trained; running `cf.py grid --main-only` "
            "(resumable; also completes any missing evaluation)")
        ok = run("a:main-grid", ["cf.py", "grid", "--main-only"], st)
        missing = [r for r in main_rids() if r not in done_runs()]
        if (ok and not missing) or st.get("a_attempts", 0) >= 1:
            if missing:
                fail(st, f"a:main grid missing {len(missing)} trained runs after a resumable rerun: {missing}")
            return
        st["a_attempts"] = st.get("a_attempts", 0) + 1
        save_state(st)


# ------------------------------------------------------------------ (b) selection on valid
def sweep_rows(split: str, n: int) -> dict:
    """{(run_id, T): row} of verified temperature points (newest wins)."""
    from cachekd.tempsweep import perprompt_file, verified
    out = {}
    for r in read_jsonl(path("runs", "tempsweep.jsonl")):
        if (r["split"] == split and r.get("n_prompts") == n and r.get("config_version") == CONFIG_VERSION
                and r.get("n_samples") == CFG["n_samples"] and r.get("max_new_tokens") == CFG["max_new_tokens"]):
            if verified(r, perprompt_file(r["run_id"], split, r["temperature"], n)):
                out[(r["run_id"], round(float(r["temperature"]), 6))] = r
    return out


def extension(vals: dict, target: float):
    """One extra temperature if `target` is not bracketed by the measured values {T: rouge}; else None.
    The step goes beyond the END temperature whose value is closer to the target (never T <= 0)."""
    if not vals:
        return None
    ys = list(vals.values())
    if min(ys) <= target <= max(ys):
        return None
    lo, hi = min(vals), max(vals)
    end = lo if abs(vals[lo] - target) <= abs(vals[hi] - target) else hi
    t = round(end - STEP if end == lo else end + STEP, 6)
    return t if t > 0 else None


def choose(vals: dict, target: float) -> float:
    """The MEASURED temperature whose value is closest to the target (ties: closer to 1.0). No extrapolation."""
    return min(vals, key=lambda T: (abs(vals[T] - target), abs(T - 1.0)))


def point(step: str, rid: str, split: str, T: float, n: int | None, st: dict) -> bool:
    args = ["cf.py", "point", "--run", rid, "--split", split, "--T", f"{T:g}"] + (["--n", str(n)] if n else [])
    return run(step, args, st)


def _vals(rid: str, temps) -> dict:
    """{T: sampled ROUGE-L} of the verified valid points of `rid` at the REGISTERED temperatures only (finite)."""
    import math
    rows = sweep_rows("valid", SEL_N)
    out = {}
    for T in temps:
        r = rows.get((rid, round(float(T), 6)))
        if r is not None and math.isfinite(r["rougeL_sampled"]):
            out[round(float(T), 6)] = r["rougeL_sampled"]
    return out


def stage_b(st: dict, final_pass: bool = False) -> None:
    if load_selection():
        say("selection already made (runs/final_selection.json)")
        return
    t8, fu = top8(1), full(1)
    for rid, temps in ((t8, SEL_TOP8), (fu, SEL_FULL)):
        for T in temps:
            point(f"b:valid {rid} T={T:g}", rid, "valid", T, SEL_N, st)
    v8, vf = _vals(t8, SEL_TOP8), _vals(fu, SEL_FULL)
    if 1.0 not in vf or 1.0 not in v8:
        fail(st, "b:selection impossible: a T = 1.0 reference point is missing on valid")
        return
    if (len(v8) < len(SEL_TOP8) or len(vf) < len(SEL_FULL)) and not final_pass:
        fail(st, f"b:selection postponed: registered valid points missing (top-8 has {sorted(v8)}, full {sorted(vf)}); "
                 "retried in pass 2")
        return
    sel = {"made": now(), "valid_prompts": SEL_N, "training_seed": 1, "config_version": CONFIG_VERSION,
           "n_samples": CFG["n_samples"], "max_new_tokens": CFG["max_new_tokens"],
           "rule": "closest measured sampled ROUGE-L; one extra step of 0.15 if not bracketed; no extrapolation",
           "complete_registered_grid": len(v8) == len(SEL_TOP8) and len(vf) == len(SEL_FULL)}
    ext_plan = st.setdefault("extension_plan", {})
    for name, rid, temps, target in (("T_top8", t8, SEL_TOP8, vf[1.0]), ("T_full", fu, SEL_FULL, v8[1.0])):
        vals = _vals(rid, temps)
        if name in ext_plan:  # decided ONCE from the complete registered grid, persisted before it runs
            ext = ext_plan[name]
        else:
            ext = extension(vals, target)
            if len(vals) == len(temps):
                ext_plan[name] = ext
                save_state(st)
        if ext is not None:
            say(f"{name}: target {target:.3f} not bracketed by {vals}; adding T = {ext:g} once", explog=True)
            point(f"b:valid {rid} T={ext:g} (extension)", rid, "valid", ext, SEL_N, st)
            vals = _vals(rid, list(temps) + [ext])
        sel[name] = choose(vals, target)
        sel[name + "_target_rougeL"] = target
        sel[name + "_measured"] = {f"{T:g}": round(v, 4) for T, v in sorted(vals.items())}
        sel[name + "_extension"] = ext
        sel[name + "_bracketed_after_extension"] = min(vals.values()) <= target <= max(vals.values())
    tmp = SELECTION.with_suffix(".tmp")
    tmp.write_text(json.dumps(sel, indent=1), encoding="utf-8")
    os.replace(tmp, SELECTION)
    say(f"temperature selection (valid, seed 1): T_top8* = {sel['T_top8']:g}, T_full* = {sel['T_full']:g} "
        f"(full KD @1.0 ROUGE-L {vf[1.0]:.2f}, top-8 @1.0 {v8[1.0]:.2f}); written to runs/final_selection.json",
        explog=True)


def load_selection():
    """The selection if it exists, parses and matches the current setup; else None (a corrupt or stale file
    is moved aside, never trusted)."""
    if not SELECTION.exists():
        return None
    try:
        sel = json.loads(SELECTION.read_text(encoding="utf-8"))
        # a selection written by the first queue version (19 Oct 14:05) lacks the setup keys: accepted as is
        ok = (sel.get("config_version", CONFIG_VERSION) == CONFIG_VERSION
              and sel.get("n_samples", CFG["n_samples"]) == CFG["n_samples"]
              and sel.get("max_new_tokens", CFG["max_new_tokens"]) == CFG["max_new_tokens"]
              and "T_top8" in sel and "T_full" in sel)
    except (OSError, ValueError):
        ok, sel = False, None
    if not ok:
        bad = SELECTION.with_suffix(f".invalid.{int(time.time())}.json")
        os.replace(SELECTION, bad)
        say(f"runs/final_selection.json was unreadable or stale: moved to {bad.name}", explog=True)
        return None
    return sel


# ------------------------------------------------------------------ (c) matched test points
def matched_points() -> list:
    sel = load_selection()
    if not sel:
        return []
    pts = []
    for s in TEST_SEEDS:
        for rid, T in ((top8(s), sel["T_top8"]), (full(s), 1.0), (top8(s), 1.0), (full(s), sel["T_full"])):
            if (rid, T) not in pts:
                pts.append((rid, T))
    return pts


def stage_c(st: dict) -> None:
    pts = matched_points()
    if not pts:
        fail(st, "c:matched test points skipped: no temperature selection")
        return
    for rid, T in pts:
        point(f"c:test {rid} T={T:g}", rid, "test", T, None, st)
    results(st)


# ------------------------------------------------------------------ (d) references + analyses
def stage_d(st: dict) -> None:
    run("d:eval base student 135m test", ["cf.py", "eval", "--run", "135m", "--split", "test"], st)
    run("d:eval teacher test", ["cf.py", "eval", "--run", "teacher", "--split", "test"], st)  # skips if done
    for script in ("calibration.py", "bootstrap.py", "length_controlled.py"):
        run(f"d:analysis {script}", [str(Path("analysis") / script)], st)
    results(st)


# ------------------------------------------------------------------ (e, f) ablations, diagnostic
def ablation_jobs() -> list:
    """(step name, cf.py job args) in the pre-registered order."""
    def j(arm, div, k, seed, ce=0.0, sft=False):
        name = f"e:{'ce0.5 ' if ce else ''}{'sftinit ' if sft else ''}{arm}{'' if k is None else k} {div} s{seed}"
        a = ["cf.py", "job", "--arm", arm, "--div", div, "--seed", str(seed)]
        a += ["--k", str(k)] if k else []
        a += ["--ce-weight", str(ce)] if ce else []
        a += ["--sft-init"] if sft else []
        return name, a
    out = []
    for s in (1, 2):
        out += [j("topk", "fkl", 8, s, ce=0.5), j("full", "fkl", None, s, ce=0.5),
                j("topk", "fkl", 8, s, sft=True), j("full", "fkl", None, s, sft=True)]
    rest = [(a, d, k) for a, d, k, _ in P.SFT_INIT_ABLATION["arms"]
            if (a, d, k) not in (("topk", "fkl", 8), ("full", "fkl", None))]
    for s in (1, 2):
        out += [j(a, d, k, s, sft=True) for a, d, k in rest]
    return out


def stage_e(st: dict) -> None:
    for i, (name, args) in enumerate(ablation_jobs()):
        st["next"] = [n for n, _ in ablation_jobs()[i + 1:]] + ["f:tailuniform k8 fkl s1"]
        run(name, args, st)
        results(st)


def stage_f(st: dict) -> None:
    st["next"] = []
    run("f:tailuniform k8 fkl s1", ["cf.py", "job", "--arm", "tailuniform", "--k", "8", "--div", "fkl",
                                     "--seed", "1"], st)
    results(st)


def other_instance() -> bool:
    return bool(_script_procs("run_final.py"))


def main() -> None:
    if other_instance():
        print("run_final.py is already running (see STATUS.md); not starting a second copy.")
        return
    keep_awake()
    st = load_state()
    st["running"] = None
    st["next"] = ["a: main grid seed 3", "b: valid selection", "c: matched test points", "d: base eval + analyses",
                  "e: ablations", "f: uniform tail"]
    save_state(st)
    say(f"run_final.py started (pid {os.getpid()})", explog=True)
    for pass_no in (1, 2):
        say(f"queue pass {pass_no}")
        st["next"] = ["b: valid selection", "c: matched test points", "d: base eval + analyses", "e: ablations",
                      "f: uniform tail"]
        stage_a(st)
        results(st)
        stage_b(st, final_pass=pass_no == 2)
        st["next"] = ["c: matched test points", "d: base eval + analyses", "e: ablations", "f: uniform tail"]
        stage_c(st)
        st["next"] = ["d: base eval + analyses", "e: ablations", "f: uniform tail"]
        stage_d(st)
        stage_e(st)
        stage_f(st)
        if pass_no == 1 and not st["failed"]:
            break
        st["failed"] = [f + " [pass 1]" if "[pass" not in f else f for f in st["failed"]]
    st["running"] = None
    st["next"] = []
    save_state(st)
    say(f"queue finished; {len(st['failed'])} failed steps (listed in STATUS.md)", explog=True)


if __name__ == "__main__":
    main()
