"""Collect qualitative examples for the professor meeting (no new metrics, nothing trained).

1. Side-by-side generations: 30 test prompts drawn at random (seed 0, not hand-picked), with the
   teacher's and each seed-1 student's greedy answer and first 3 temperature-1 samples, read from
   cf_data/gens (the same files the paper's numbers come from).
2. "Tail peek": teacher-forced on the gold answer, positions where the gold token falls OUTSIDE the
   teacher's top-8 (the token a top-8 cache never shows the student). For each, the probability
   the teacher and each student give to the gold token and to the teacher's ranks 9-16.

Run from the repo root:  .venv\\Scripts\\python.exe presentation\\make_examples.py
Writes presentation/examples.json.
"""
import json
import random
import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cachekd import data as D  # noqa: E402
from cachekd.models import load_model, load_tokenizer, response_logits  # noqa: E402

GENS = Path("cf_data/gens")
ARMS = {  # label -> run id (seed 1, forward KL, main grid)
    "SFT (no teacher)": "sft_135m_nall_s1_v1",
    "Full KD": "full_fkl_135m_nall_s1_v1",
    "Top-8 cache": "topk_k8_fkl_135m_nall_s1_v1",
    "Top-128 cache": "topk_k128_fkl_135m_nall_s1_v1",
    "Tail bucket (k=8)": "tailbucket_k8_fkl_135m_nall_s1_v1",
    "RS-KD": "rs_fkl_135m_nall_s1_v1",
}
PEEK_ARMS = ["Full KD", "Top-8 cache", "Tail bucket (k=8)", "RS-KD"]
N_PROMPTS, N_SAMPLES, N_PEEK, K = 30, 3, 10, 8


def read(p):
    return {r["id"]: r for r in map(json.loads, open(p, encoding="utf-8"))}


def generations(rows):
    teacher = read(GENS / "teacher__test__n500__s0.jsonl")
    students = {lab: read(GENS / f"{rid}__test__n500__s0.jsonl") for lab, rid in ARMS.items()}
    picked = random.Random(0).sample(rows, N_PROMPTS)
    out = []
    for r in picked:
        models = {"Teacher (1.7B)": teacher[r["id"]]} | {lab: g[r["id"]] for lab, g in students.items()}
        out.append({"id": r["id"], "instruction": r["instruction"], "context": r.get("context") or "",
                    "category": r.get("category"), "gold": r["response"],
                    "outputs": {m: {"greedy": g["greedy"], "samples": g["samples"][:N_SAMPLES]}
                                for m, g in models.items()}})
    return out


@torch.no_grad()
def probs_on_gold(model, enc, tok, who):
    b = D.collate([enc], tok.pad_token_id, who)
    return torch.softmax(response_logits(model, b), -1).cpu()  # (len(resp), V), fp32


def tail_peek(rows, tok):
    cand = random.Random(1).sample(rows, 80)
    encs = [D.encode(r, tok) for r in cand]
    teacher = load_model("teacher")
    found = []
    for r, e in zip(cand, encs):
        p = probs_on_gold(teacher, e, tok, "teacher")
        gold = torch.tensor(e["resp"])
        rank = (p > p[torch.arange(len(gold)), gold][:, None]).sum(-1)  # 0 = teacher's top token
        # skip the first token and the end token; keep the most likely tail gold token of this example
        ok = [j for j in range(1, len(gold) - 1) if rank[j] >= K]
        if not ok:
            continue
        j = max(ok, key=lambda j: p[j, gold[j]].item())
        top = p[j].topk(16)
        found.append({"r": r, "e": e, "j": j, "gold": int(gold[j]), "rank": int(rank[j]) + 1,
                      "top_ids": top.indices.tolist(), "teacher": p[j]})
        if len(found) == N_PEEK:
            break
    del teacher
    torch.cuda.empty_cache()

    stud = {}
    for lab in PEEK_ARMS:
        m = load_model(str(Path("cf_data/ckpt") / ARMS[lab]))
        stud[lab] = [probs_on_gold(m, f["e"], tok, "student")[f["j"]] for f in found]
        del m
        torch.cuda.empty_cache()

    dec = lambda i: tok.decode([i])  # noqa: E731
    out = []
    for n, f in enumerate(found):
        dists = {"Teacher": f["teacher"]} | {lab: stud[lab][n] for lab in PEEK_ARMS}
        show = f["top_ids"][:K] + [i for i in f["top_ids"][K:]] + ([f["gold"]] if f["gold"] not in f["top_ids"] else [])
        ctx = f["e"]["resp"][max(0, f["j"] - 25):f["j"]]
        out.append({"id": f["r"]["id"], "instruction": f["r"]["instruction"],
                    "context_before": tok.decode(ctx), "gold_token": dec(f["gold"]), "gold_rank": f["rank"],
                    "tokens": [{"tok": dec(i), "teacher_rank": int((f["teacher"] > f["teacher"][i]).sum()) + 1,
                                "is_gold": i == f["gold"],
                                "p": {m: round(d[i].item(), 5) for m, d in dists.items()}} for i in show],
                    "mass_outside_teacher_top8": {m: round(1 - d[f["top_ids"][:K]].sum().item(), 4)
                                                  for m, d in dists.items()}})
    return out


if __name__ == "__main__":
    rows = D.load_split("test")
    tok = load_tokenizer("135m")
    res = {"generations": generations(rows), "tail_peek": tail_peek(rows, tok),
           "notes": {"prompts": f"{N_PROMPTS} test prompts, random.Random(0).sample; seed-1 students",
                     "samples": "temperature 1, no top-k/top-p, max 256 new tokens (cf_data/gens)",
                     "peek": f"gold token outside teacher top-{K}; first {N_PEEK} of 80 random test items "
                             "(random.Random(1)) that have one; the most teacher-likely such token per item"}}
    Path("presentation/examples.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(len(res["generations"]), "prompts,", len(res["tail_peek"]), "peek positions")
