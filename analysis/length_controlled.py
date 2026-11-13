"""Length-controlled diversity, computed post hoc from the saved generations (CPU only, safe while the
grid runs). Diversity metrics fall with answer length, and our arms differ in length, so H1 must also hold:

  first32   : every sample cut to its first 32 words -> distinct-2, Self-BLEU, semantic diversity
  finished  : only samples that ended with the end token (prompts with >= 2 such samples)

`ended` is read from the gens file when present (evaluations after 18 Oct 13:30); for older files it is
recovered by re-tokenising: a sample that hit max_new_tokens re-encodes to ~256 tokens, a finished one to
fewer (the threshold is validated against each evaluation's recorded ended_rate_sampled).

  .venv\\Scripts\\python.exe analysis\\length_controlled.py [--splits test selfinst vicuna]
Writes $CF_ROOT/runs/length_controlled.jsonl (one row per evaluation; resumable).
"""
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cachekd import metrics as M  # noqa: E402
from cachekd.config import CFG, MODELS  # noqa: E402

ROOT = Path(os.environ.get("CF_ROOT", Path(__file__).resolve().parent.parent / "cf_data"))
N_WORDS = 32
CUT_TOKENS = CFG["max_new_tokens"] - 6  # >= this many re-encoded tokens counts as "hit the length limit"


def jsonl(p: Path) -> list:
    # split on "\n" only: str.splitlines() also breaks on U+2028 etc., which occur inside generated text
    return [json.loads(x) for x in p.read_text(encoding="utf-8").split("\n") if x.strip()] if p.exists() else []


def first_words(t: str, n: int = N_WORDS) -> str:
    return " ".join(M.normalize_answer(t).split()[:n])


def group_div(groups: list, emb) -> dict:
    """distinct-2, Self-BLEU and semantic diversity over per-prompt sample groups (groups with >= 2 texts)."""
    groups = [[s for s in g if s.strip()] for g in groups]
    groups = [g for g in groups if len(g) >= 2]
    if not groups:
        return {"n_prompts": 0}
    flat = [s for g in groups for s in g]
    vecs = emb.encode(flat, batch_size=64, convert_to_numpy=True, show_progress_bar=False)
    out, i = [], 0
    for g in groups:
        out.append(vecs[i:i + len(g)])
        i += len(g)
    return {"n_prompts": len(groups), "distinct2": M.distinct_n_per_prompt(groups, 2),
            "self_bleu": M.mean_self_bleu(groups), "semantic_div": M.mean_semantic_diversity(out)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--splits", nargs="+", default=["test", "selfinst", "vicuna"])
    a = ap.parse_args()
    import torch
    torch.set_num_threads(4)  # leave CPU for the running grid
    from sentence_transformers import SentenceTransformer
    from transformers import AutoTokenizer
    emb = SentenceTransformer(MODELS["embedder"], device="cpu")
    tok = AutoTokenizer.from_pretrained(MODELS["135m"])
    out_f = ROOT / "runs" / "length_controlled.jsonl"
    done = {(r["run_id"], r["split"], r["n_prompts"], r["seed"]) for r in jsonl(out_f)}
    evals = {}
    for r in jsonl(ROOT / "runs" / "evals.jsonl"):
        evals[(r["run_id"], r["split"], r["n_prompts"], r["seed"])] = r  # newest row wins
    for (run, split, n, seed), ev in sorted(evals.items()):
        if split not in a.splits or (run, split, n, seed) in done:
            continue
        g_f = ROOT / "gens" / f"{run}__{split}__n{n}__s{seed}.jsonl"
        if not g_f.exists():
            continue
        rows = jsonl(g_f)
        samples = [r["samples"] for r in rows]
        if all("samples_ended" in r for r in rows):
            ended, src = [r["samples_ended"] for r in rows], "saved"
        else:
            lens = [[len(tok(s, add_special_tokens=False)["input_ids"]) for s in g] for g in samples]
            ended, src = [[n_ < CUT_TOKENS for n_ in g] for g in lens], "re-tokenised"
        rate = float(np.mean([e for g in ended for e in g]))
        rec = {"run_id": run, "split": split, "n_prompts": n, "seed": seed, "ended_source": src,
               "ended_rate_recovered": rate, "ended_rate_recorded": ev.get("ended_rate_sampled")}
        for k, v in group_div([[first_words(s) for s in g] for g in samples], emb).items():
            rec[f"first{N_WORDS}_{k}"] = v
        for k, v in group_div([[s for s, e in zip(g, eg) if e] for g, eg in zip(samples, ended)], emb).items():
            rec[f"finished_{k}"] = v
        with open(out_f, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"{run} {split}: ended {rate:.3f} (recorded {ev.get('ended_rate_sampled')}) | first32 sem "
              f"{rec.get('first32_semantic_div', float('nan')):.3f} | finished sem "
              f"{rec.get('finished_semantic_div', float('nan')):.3f} on {rec.get('finished_n_prompts')} prompts",
              flush=True)


if __name__ == "__main__":
    main()
