"""Sampling-temperature sweep on finished checkpoints (authors, 19 Oct 2026; analysis plan in HYPOTHESES.md).

For each planned (run, T) (`sweepplan.PLAN`): 8 samples per Dolly-test prompt at temperature T (no top-k/top-p,
sampling seed 0, the same batch layout as the main evaluation, so T = 1.0 reproduces it up to GPU
non-determinism), scored with sampled ROUGE-L, semantic diversity, Self-BLEU, distinct-2, finish rate, mean
length, empty rate and the reference-free quality measure: the TEACHER's mean log-likelihood per token of the
student's samples (teacher_lp_seq = mean over samples of the per-sample mean, the definition used by the main
evaluation's `precision_teacher_lp`; teacher_lp_tok = token-weighted).
Per-prompt values are saved for the paired bootstrap in analysis/frontier.py. No training; resumable per point.

Compatibility: a grid process started before the plan change calls the legacy `sample_eval` once per old sweep
point. It now completes the whole registered plan (idempotent), so such a process runs the plan at the right
place in its queue (after the main grid, before the ablations) without a restart.
"""
import hashlib
import json
import os

import numpy as np
import torch

from . import data as D
from . import metrics as M
from .config import CFG, CONFIG_VERSION, MODELS
from .evaluate import model_ref
from .generate import generate, mean_logprob
from .models import load_model, load_tokenizer
from .utils import Timer, append_jsonl, device, git_state, log, path, read_jsonl, write_jsonl

PER_PROMPT = ("rougeL_sampled", "semantic_div", "self_bleu", "teacher_lp", "teacher_lp_tok", "len", "ended")


def plan_points() -> list:
    from . import sweepplan
    return sweepplan.points()


def _same_setup(r: dict, run: str, split: str, temperature: float, seed: int, n: int) -> bool:
    """A finished row counts only if it was made with the same prompts, sampling and config."""
    return (r["run_id"] == run and r["split"] == split and abs(r["temperature"] - temperature) < 1e-9
            and r["seed"] == seed and r.get("n_prompts") == n and r.get("n_samples") == CFG["n_samples"]
            and r.get("max_new_tokens") == CFG["max_new_tokens"] and r.get("config_version") == CONFIG_VERSION)


def perprompt_file(run: str, split: str, temperature: float, n: int | None = None):
    tag = "" if n in (None, CFG["eval_prompts"]) else f"__n{n}"  # non-default prompt counts get their own file
    return path("runs", "sweep_perprompt", f"{run}__{split}{tag}__T{temperature:g}.json")


def setup_of(run: str, split: str, temperature: float, seed: int, n: int) -> dict:
    return {"run_id": run, "split": split, "temperature": temperature, "seed": seed, "n_prompts": n,
            "n_samples": CFG["n_samples"], "max_new_tokens": CFG["max_new_tokens"], "config_version": CONFIG_VERSION}


def verified(row: dict, f) -> bool:
    """True iff the per-prompt file exists, hashes to what the result row recorded, and carries the same setup:
    the row and its artifact are bound together (a stale or foreign file never counts)."""
    if not f.exists() or "perprompt_sha256" not in row:
        return False
    raw = f.read_bytes()
    if hashlib.sha256(raw).hexdigest() != row["perprompt_sha256"]:
        return False
    try:
        d = json.loads(raw)
    except ValueError:
        return False
    return d.get("setup") == {k: row[k] for k in ("run_id", "split", "temperature", "seed", "n_prompts", "n_samples",
                                                  "max_new_tokens", "config_version")}


def _free() -> None:
    """Release GPU memory between the student, the teacher and the embedder (HF models hold reference cycles)."""
    import gc
    gc.collect()
    torch.cuda.empty_cache()


_FAILED_IN_PROCESS: set = set()  # a point that failed in this process is not retried by later run_plan calls


def _finite(a) -> np.ndarray:
    a = np.array(a, dtype=float)
    a[~np.isfinite(a)] = np.nan  # +-inf counts as missing everywhere downstream
    return a


def sample_point(run: str, split: str, temperature: float, seed: int = 0, n: int | None = None) -> dict:
    """One (run, T) point on the first `n` prompts of `split` (default CFG["eval_prompts"]). Returns the result
    row, or {} if an identical point is already done."""
    out_f = path("runs", "tempsweep.jsonl")
    rows = D.load_eval_set(split, n or CFG["eval_prompts"])
    pp_f = perprompt_file(run, split, temperature, len(rows))
    if any(_same_setup(r, run, split, temperature, seed, len(rows)) and verified(r, pp_f) for r in read_jsonl(out_f)):
        log(f"skip tempsweep {run} {split} T={temperature:g}: done (artifact verified)")
        return {}
    name, side = model_ref(run)
    assert side == "student", "temperature sweep is for students"
    K = CFG["n_samples"]
    tok = load_tokenizer("135m")
    prompts = [D.encode_ids(D.student_prompt(r), tok) for r in rows]
    model = load_model(name)
    with Timer() as tg:
        samples = generate(model, tok, prompts, greedy=False, n=K, max_new=CFG["max_new_tokens"],
                           batch=CFG["gen_batch"], temperature=temperature, seed=seed)
    del model
    _free()
    txt = [[s["text"] for s in g] for g in samples]
    ids = [[s["ids"] for s in g] for g in samples]
    # reference-free quality: the teacher's log-likelihood of each sample (teacher prompt, same as evaluation)
    teacher = load_model("teacher")
    t_prompts = [D.encode_ids(D.teacher_prompt(r, tok), tok) for r in rows]
    flat_p = [p for p in t_prompts for _ in range(K)]
    flat_r = [x for g in ids for x in g]
    lp = _finite(mean_logprob(teacher, tok, flat_p, flat_r, "teacher")).reshape(len(rows), K)
    del teacher
    _free()
    lens = np.array([[len(x) for x in g] for g in ids], dtype=float)
    ended = np.array([[s["ended"] for s in g] for g in samples], dtype=float)
    from sentence_transformers import SentenceTransformer
    emb = SentenceTransformer(MODELS["embedder"], device=str(device()))
    vecs = emb.encode([s for g in txt for s in g], batch_size=128, convert_to_numpy=True, show_progress_bar=False)
    del emb
    _free()
    pp = {k: [] for k in PER_PROMPT}
    for i, (g, rf) in enumerate(zip(txt, [r["refs"] for r in rows])):
        v = [x for x, s in zip(vecs[i * K:(i + 1) * K], g) if s.strip()]
        pp["rougeL_sampled"].append(float(np.mean([M.rouge_l(s, rf) for s in g])))
        pp["semantic_div"].append(M.semantic_diversity(np.stack(v) if v else np.zeros((1, vecs.shape[1]))))
        pp["self_bleu"].append(M.self_bleu(g))
        fin = np.isfinite(lp[i])
        pp["teacher_lp"].append(float(np.mean(lp[i][fin])) if fin.any() else float("nan"))  # sample-weighted within a prompt
        pp["teacher_lp_tok"].append(float(np.sum(lp[i][fin] * lens[i][fin]) / lens[i][fin].sum()) if fin.any()
                                    else float("nan"))  # token-weighted within a prompt
        pp["len"].append(float(lens[i].mean()))
        pp["ended"].append(float(ended[i].mean()))
    nm = lambda a: float(np.nanmean(np.array(a, dtype=float)))  # noqa: E731
    res = {"run_id": run, "split": split, "temperature": temperature, "seed": seed, "n_prompts": len(rows),
           "n_samples": K, "max_new_tokens": CFG["max_new_tokens"], "gen_s": tg.s,
           "rougeL_sampled": nm(pp["rougeL_sampled"]), "semantic_div": nm(pp["semantic_div"]),
           "self_bleu": nm(pp["self_bleu"]), "distinct2": M.distinct_n_per_prompt(txt, 2),
           "len_sampled": float(lens.mean()), "ended_rate_sampled": float(ended.mean()),
           "empty_rate_sampled": float(np.mean([not s.strip() for g in txt for s in g])),
           # quality definitions: seq = mean over all finite samples (the main evaluation's precision_teacher_lp);
           # prompt = mean over prompts of per-prompt means (what the frontier analysis uses); tok = token-weighted
           "teacher_lp_seq": float(np.nanmean(lp)), "teacher_lp_prompt": float(np.nanmean(pp["teacher_lp"])),
           "teacher_lp_tok": float(np.nansum(lp * lens) / lens[np.isfinite(lp)].sum()),
           "n_missing_lp": int((~np.isfinite(lp)).sum()),
           "n_prompts_missing_semantic": int(np.isnan(pp["semantic_div"]).sum()),
           "n_prompts_missing_self_bleu": int(np.isnan(pp["self_bleu"]).sum())}
    pp_f.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"setup": setup_of(run, split, temperature, seed, len(rows)),
                          "ids": [r["id"] for r in rows], **{k: list(_finite(v)) for k, v in pp.items()}})
    tmp = pp_f.with_suffix(f".{os.getpid()}.tmp")  # unique per process
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, pp_f)
    res["perprompt_sha256"] = hashlib.sha256(pp_f.read_bytes()).hexdigest()
    write_jsonl(path("gens", f"{run}__{split}__n{len(rows)}__s{seed}__T{temperature:g}.jsonl"),
                [{"id": r["id"], "samples": g, "samples_ended": [bool(s["ended"]) for s in sg]}
                 for r, g, sg in zip(rows, txt, samples)])
    res.update({"config_version": CONFIG_VERSION, **git_state()})
    append_jsonl(out_f, res)  # last: a point counts as done only after its per-prompt file exists
    log(f"tempsweep {run} T={temperature:g}: " + ", ".join(f"{k}={v:.4g}" for k, v in res.items()
                                                           if isinstance(v, float) and k not in ("temperature",)))
    return res


def run_plan(points=None, split: str | None = None) -> list:
    """Run every planned point not yet done, in plan order. A failing point is logged to runs/failed.jsonl and
    the next point still runs (every failure is reported). Idempotent."""
    from . import sweepplan
    pts = plan_points() if points is None else points
    split = sweepplan.SPLIT if split is None else split
    done = []
    for rid, T in pts:
        if (rid, T) in _FAILED_IN_PROCESS:
            continue
        try:
            r = sample_point(rid, split, T)
            if r:
                done.append(r)
        except Exception as e:  # noqa: BLE001
            _FAILED_IN_PROCESS.add((rid, T))
            append_jsonl(path("runs", "failed.jsonl"), {"run_id": rid, "temperature": T, "stage": "tempsweep",
                                                        "error": repr(e)})
            log(f"FAILED tempsweep {rid} T={T:g}: {e!r}")
        finally:
            import gc
            gc.collect()
            torch.cuda.empty_cache()
    return done


def sample_eval(run: str, split: str, temperature: float, seed: int = 0) -> dict:
    """LEGACY entry point (see the module docstring): completes the whole registered plan; returns {}."""
    run_plan()
    return {}
