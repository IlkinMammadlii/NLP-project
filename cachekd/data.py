"""Dolly splits, prompt formats, tokenisation and batching.

Student input  = MiniLLM/Alpaca prompt  + response tokens + <|im_end|>
Teacher input  = teacher chat template  + the SAME response tokens + <|im_end|>
The response ids are tokenised once and appended to both prompts, so the i-th predicted
response position of the student and of the teacher are the same token prediction.
"""
import random

import torch

from .config import CFG
from .utils import md5_ids, path, read_jsonl, write_jsonl, log

END_TOKEN = "<|im_end|>"

PROMPT_NO_INPUT = (
    "Below is an instruction that describes a task. Write a response that appropriately completes the request."
    "\n\n### Instruction:\n{instruction}\n\n### Response:\n"
)
PROMPT_WITH_INPUT = (
    "Below is an instruction that describes a task, paired with an input that provides further context. "
    "Write a response that appropriately completes the request."
    "\n\n### Instruction:\n{instruction}\n\n### Input:\n{context}\n\n### Response:\n"
)


def student_prompt(ex: dict) -> str:
    ctx = (ex.get("context") or "").strip()
    tpl = PROMPT_WITH_INPUT if ctx else PROMPT_NO_INPUT
    return tpl.format(instruction=ex["instruction"].strip(), context=ctx)


def teacher_prompt(ex: dict, tok) -> str:
    if CFG["teacher_format"] == "plain":
        return student_prompt(ex)
    ctx = (ex.get("context") or "").strip()
    user = ex["instruction"].strip() + (f"\n\n{ctx}" if ctx else "")
    return tok.apply_chat_template([{"role": "user", "content": user}], tokenize=False, add_generation_prompt=True)


def end_id(tok) -> int:
    i = tok.convert_tokens_to_ids(END_TOKEN)
    assert i is not None and i != tok.unk_token_id, f"{END_TOKEN} missing from tokenizer"
    return i


def encode_ids(text: str, tok) -> list:
    return tok(text, add_special_tokens=False)["input_ids"]


def encode(ex: dict, tok, response_ids: list | None = None) -> dict:
    """Token ids for one example; prompt + response is cut at max_len (a cut response has no end token).
    `response_ids` (seq-KD) are the teacher's generated ids, ending in the end token only if it stopped."""
    sp = encode_ids(student_prompt(ex), tok)
    r = encode_ids(ex["response"].strip(), tok) + [end_id(tok)] if response_ids is None else list(response_ids)
    return {"s_prompt": sp, "t_prompt": encode_ids(teacher_prompt(ex, tok), tok),
            "resp": r[:max(1, CFG["max_len"] - len(sp))]}


# ---------------------------------------------------------------- splits
def build_splits(tok, rows: list | None = None) -> dict:
    """Shuffle Dolly with split_seed, drop prompts over max_prompt_len (MiniLLM), then test / valid / train."""
    if rows is None:
        from datasets import load_dataset
        rows = [dict(r) for r in load_dataset(CFG["dolly"], split="train")]
    for i, r in enumerate(rows):
        r["id"] = f"dolly_{i}"
    order = list(range(len(rows)))
    random.Random(CFG["split_seed"]).shuffle(order)
    kept, dropped = [], 0
    for i in order:
        e = encode(rows[i], tok)
        if len(e["s_prompt"]) > CFG["max_prompt_len"] or not rows[i]["response"].strip():
            dropped += 1
            continue
        kept.append(rows[i])
    nt, nv = CFG["n_test"], CFG["n_valid"]
    splits = {"test": kept[:nt], "valid": kept[nt:nt + nv], "train": kept[nt + nv:]}
    # leakage guard: drop TRAIN items sharing an instruction or a non-empty context with valid/test
    # (Dolly has near-duplicate questions and several questions per source passage)
    from .metrics import normalize_answer as norm
    held = {norm(r["instruction"]) for r in splits["test"] + splits["valid"]}
    held_ctx = {norm(r["context"]) for r in splits["test"] + splits["valid"] if (r.get("context") or "").strip()}
    n_before = len(splits["train"])
    splits["train"] = [r for r in splits["train"] if norm(r["instruction"]) not in held
                       and not ((r.get("context") or "").strip() and norm(r["context"]) in held_ctx)]
    info = {"n_raw": len(rows), "dropped_long_prompt_or_empty": dropped,
            "dropped_train_overlap_with_eval": n_before - len(splits["train"])}
    for name, rs in splits.items():
        write_jsonl(path("data", f"{name}.jsonl"), rs)
        info[name] = {"n": len(rs), "md5": md5_ids([r["id"] for r in rs])}
    write_jsonl(path("data", "data_info.jsonl"), [info])
    log(f"splits: {info}")
    return info


def load_split(name: str, n: int | None = None) -> list:
    rows = read_jsonl(path("data", f"{name}.jsonl"))
    assert rows, f"no data for split {name}: run `python cf.py data` first"
    return rows[:n] if n else rows


# ---------------------------------------------------------------- batching
def collate(encs: list, pad_id: int, who: str) -> dict:
    """Right-padded batch for teacher-forced training/scoring.

    Returns input_ids, attention_mask and the flat (row, position) indices of every response
    prediction, in example order then response order: logits[row, pos] predicts target[j].
    """
    key = "s_prompt" if who == "student" else "t_prompt"
    seqs = [e[key] + e["resp"] for e in encs]
    L = max(len(s) for s in seqs)
    ids = torch.full((len(seqs), L), pad_id, dtype=torch.long)
    mask = torch.zeros((len(seqs), L), dtype=torch.long)
    rows, pos, tgt = [], [], []
    for b, (e, s) in enumerate(zip(encs, seqs)):
        ids[b, :len(s)] = torch.tensor(s)
        mask[b, :len(s)] = 1
        p0 = len(e[key])
        for j, t in enumerate(e["resp"]):
            rows.append(b)
            pos.append(p0 - 1 + j)
            tgt.append(t)
    return {"input_ids": ids, "attention_mask": mask, "rows": torch.tensor(rows),
            "pos": torch.tensor(pos), "targets": torch.tensor(tgt)}


def left_pad(prompts: list, pad_id: int) -> tuple:
    L = max(len(p) for p in prompts)
    ids = torch.full((len(prompts), L), pad_id, dtype=torch.long)
    mask = torch.zeros((len(prompts), L), dtype=torch.long)
    for b, p in enumerate(prompts):
        ids[b, L - len(p):] = torch.tensor(p)
        mask[b, L - len(p):] = 1
    return ids, mask


# ---------------------------------------------------------------- other eval sets
# MiniLLM's own evaluation files (the HF copies the MiniLLM repo points to). `datasets.load_dataset` fails
# on them (valid.jsonl and valid.txt in one repo), so the raw jsonl is downloaded and parsed here.
# (repo, file, pinned commit, expected rows). Pinned on 17 Oct 2026 so every run sees the same data.
EXTERNAL = {"selfinst": ("MiniLLM/self-inst", "valid.jsonl", "ad9edcc55ec5623f6d9d2472963bda3701681af8", 242),
            "vicuna": ("MiniLLM/Vicuna", "valid.jsonl", "05a18702266a6c51547034268d6d58bce214bd77", 80),
            # S-NI, gold responses of 11+ words (MiniLLM's main-table subset)
            "sni": ("MiniLLM/sinst", "11_/valid.jsonl", "84b8356a5b631002f92ad73adebd5ac9db5612cd", 1694)}
EVAL_SETS = ("valid", "test") + tuple(EXTERNAL)


def build_external(name: str) -> list:
    """Download one MiniLLM eval file once, normalise it to our row format, keep it under $CF_ROOT/data."""
    import json as _json
    from huggingface_hub import hf_hub_download
    import hashlib
    repo, fn, rev, n_expected = EXTERNAL[name]
    local = hf_hub_download(repo, fn, repo_type="dataset", revision=rev)
    raw = [_json.loads(line) for line in open(local, encoding="utf-8") if line.strip()]
    assert len(raw) == n_expected, f"{name}: {len(raw)} rows, expected {n_expected}"
    sha = hashlib.sha256(open(local, "rb").read()).hexdigest()
    rows, prompt_mismatch = [], 0
    for i, r in enumerate(raw):
        refs = r["output"] if isinstance(r["output"], list) else [r["output"]]
        row = {"id": f"{name}_{i}", "instruction": r["instruction"], "context": r.get("input") or "",
               "response": refs[0], "refs": refs, "source_category": r.get("topic") or r.get("category")}
        prompt_mismatch += student_prompt(row) != r.get("prompt", student_prompt(row))
        rows.append(row)
    write_jsonl(path("data", f"{name}.jsonl"), rows)  # atomic (tmp file + rename)
    write_jsonl(path("data", f"{name}.source.jsonl"), [{"repo": repo, "file": fn, "revision": rev, "sha256": sha,
                                                        "rows": len(rows), "prompt_mismatch": prompt_mismatch}])
    log(f"eval set {name}: {len(rows)} rows from {repo}/{fn}@{rev[:8]} sha256 {sha[:12]}; prompts differing "
        f"from MiniLLM's (trailing whitespace only, checked 17 Oct): {prompt_mismatch}")
    return rows


def load_eval_set(name: str, n: int | None = None) -> list:
    """Rows with instruction / context / response and `refs` (all references)."""
    if name in ("valid", "test"):
        rows = load_split(name, n)
        for r in rows:
            r["refs"] = [r["response"]]
        return rows
    if name not in EXTERNAL:
        raise ValueError(f"unknown eval set {name}; choose from {EVAL_SETS}")
    rows = read_jsonl(path("data", f"{name}.jsonl"))
    if len(rows) != EXTERNAL[name][3]:
        rows = build_external(name)
    return rows[:n] if n else rows
