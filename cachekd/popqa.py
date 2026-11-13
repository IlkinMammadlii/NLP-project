"""Long-tail knowledge probe: PopQA (Mallen et al., 2023) accuracy by subject popularity, restricted
to questions the teacher answers correctly (the brief: we measure what the student fails to inherit).

Correct = any normalised gold alias is a substring of the normalised greedy answer (Mallen et al.).
Buckets: quantiles of log10(s_pop) over the whole dataset, fixed once and stored with the teacher pass.
"""
import json

import numpy as np

from . import data as D
from .config import CFG
from .generate import generate
from .metrics import normalize_answer
from .models import load_model, load_tokenizer
from .utils import Timer, append_jsonl, git_state, log, path, read_jsonl, write_jsonl

INSTRUCTION = "Answer the question with a short phrase.\n\nQuestion: {q}"
N_BUCKETS = 5
MAX_NEW = 24


def load_popqa() -> list:
    from datasets import load_dataset
    rows = []
    for r in load_dataset("akariasai/PopQA", split="test"):
        rows.append({"id": f"popqa_{r['id']}", "question": r["question"], "s_pop": int(r["s_pop"]),
                     "answers": json.loads(r["possible_answers"]), "prop": r["prop"]})
    return rows


def as_example(r: dict) -> dict:
    return {"instruction": INSTRUCTION.format(q=r["question"]), "context": "", "response": ""}


def is_correct(answer: str, golds: list) -> bool:
    a = f" {normalize_answer(answer)} "
    return any(f" {g} " in a for g in (normalize_answer(x) for x in golds) if g)


def bucket_edges(rows: list) -> list:
    lp = np.log10(np.array([r["s_pop"] for r in rows]) + 1)
    return np.quantile(lp, np.linspace(0, 1, N_BUCKETS + 1))[1:-1].tolist()


def bucket(s_pop: int, edges: list) -> int:
    return int(np.searchsorted(edges, np.log10(s_pop + 1), side="right"))


def teacher_pass(n: int | None = None) -> None:
    """Teacher greedy answers to every PopQA question; keeps the bucket edges. Resumable."""
    out = path("popqa", "teacher.jsonl")
    rows = load_popqa()[:n] if n else load_popqa()
    meta_f = path("popqa", "meta.json")
    if not meta_f.exists():
        meta_f.write_text(json.dumps({"edges_log10_pop": bucket_edges(load_popqa()), "n_buckets": N_BUCKETS}))
    edges = json.loads(meta_f.read_text())["edges_log10_pop"]
    have = {r["id"] for r in read_jsonl(out)}
    todo = [r for r in rows if r["id"] not in have]
    if todo:
        tok = load_tokenizer("135m")
        teacher = load_model("teacher")
        prompts = [D.encode_ids(D.teacher_prompt(as_example(r), tok), tok) for r in todo]
        for c0 in range(0, len(todo), 1024):
            with Timer() as tm:
                gens = generate(teacher, tok, prompts[c0:c0 + 1024], greedy=True, n=1, max_new=MAX_NEW, batch=64,
                                static=True)
            for r, g in zip(todo[c0:c0 + 1024], gens):
                append_jsonl(out, {**r, "teacher_answer": g[0]["text"], "bucket": bucket(r["s_pop"], edges),
                                   "teacher_correct": is_correct(g[0]["text"], r["answers"])})
            log(f"popqa teacher {c0 + len(gens)}/{len(todo)} ({tm.s}s)")
    res = read_jsonl(out)
    by_b = {}
    for r in res:
        by_b.setdefault(r["bucket"], []).append(r["teacher_correct"])
    log("popqa teacher accuracy by bucket: " +
        ", ".join(f"b{b}: {np.mean(v):.3f} (n={len(v)}, kept {sum(v)})" for b, v in sorted(by_b.items())))


def evaluate_popqa(run: str) -> dict:
    """Student accuracy by popularity bucket on the teacher-correct questions."""
    from .evaluate import model_ref
    rows = [r for r in read_jsonl(path("popqa", "teacher.jsonl")) if r["teacher_correct"]]
    assert rows, "run `cf.py popqa-teacher` first"
    done = {r["run_id"] for r in read_jsonl(path("runs", "popqa.jsonl"))}
    if run in done:
        log(f"skip popqa {run}: done")
        return {}
    name, side = model_ref(run)
    tok = load_tokenizer("135m")
    model = load_model(name)
    prompts = [D.encode_ids(D.teacher_prompt(as_example(r), tok) if side == "teacher"
                            else D.student_prompt(as_example(r)), tok) for r in rows]
    gens = generate(model, tok, prompts, greedy=True, n=1, max_new=MAX_NEW, batch=64)  # short prompts and answers
    correct = [is_correct(g[0]["text"], r["answers"]) for g, r in zip(gens, rows)]
    res = {"run_id": run, "n": len(rows), "acc": float(np.mean(correct))}
    for b in range(N_BUCKETS):
        m = [c for c, r in zip(correct, rows) if r["bucket"] == b]
        res[f"acc_b{b}"] = float(np.mean(m)) if m else float("nan")
        res[f"n_b{b}"] = len(m)
    write_jsonl(path("gens", f"{run}__popqa.jsonl"),
                [{"id": r["id"], "answer": g[0]["text"], "correct": c} for r, g, c in zip(rows, gens, correct)])
    append_jsonl(path("runs", "popqa.jsonl"), {**res, **git_state()})
    log(f"popqa {run}: " + ", ".join(f"{k}={v:.3f}" for k, v in res.items() if isinstance(v, float)))
    return res
