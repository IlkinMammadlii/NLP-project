"""All experiment settings in one place. Any change that affects results bumps CONFIG_VERSION
and gets a dated note in EXPERIMENT_LOG.md (new run ids, old results are never mixed in)."""

CONFIG_VERSION = "v1"

MODELS = {
    "teacher": "HuggingFaceTB/SmolLM2-1.7B-Instruct",
    "135m": "HuggingFaceTB/SmolLM2-135M",
    "360m": "HuggingFaceTB/SmolLM2-360M",
    "embedder": "sentence-transformers/all-MiniLM-L6-v2",
}

CFG = {
    # --- data (MiniLLM protocol: 1k valid / 0.5k test / rest train; drop prompts > 256 tokens) ---
    "dolly": "databricks/databricks-dolly-15k",
    "split_seed": 1234,
    "n_valid": 1000,
    "n_test": 500,
    "max_prompt_len": 256,    # longer student prompts are DROPPED (MiniLLM process_data_dolly.py)
    "max_len": 512,           # student prompt + response; longer responses are TRUNCATED (no end token)
    "teacher_format": "chat",  # teacher sees its chat template; student sees the MiniLLM/Alpaca prompt.
                               # Response tokens are identical, so positions align exactly.
    # --- cache ---
    "cache_k": 128,           # top-k stored once; smaller k are slices
    "rs_samples": 50,         # Random Sampling KD draws per position, tau = 1 (Anshumann et al.: N=50, ~12 unique)
    "cache_batch": 8,
    # --- training (identical for every arm; only the teacher signal changes) ---
    "lr": 2e-4,               # pilot LR; replaced by protocol.FROZEN_LR once the dev sweep has frozen it
    "weight_decay": 0.01,
    "batch_size": 16,         # optimizer batch
    "micro_batch": 4,         # grad accumulation = batch_size / micro_batch
    "micro_batch_online": 2,  # with the teacher in memory: micro 4 peaked at 7.2 GB and spilled (33 min vs 13);
                              # same optimizer batch and token-mean loss, so the math is unchanged
    "epochs": 3,
    "warmup_frac": 0.05,
    "grad_clip": 1.0,
    "ce_weight": 0.0,         # extra CE on the gold token for KD arms (0 = pure KD; ablation knob)
    "kd_temperature": 1.0,    # fixed: cached probabilities are stored at tau = 1
    "grad_checkpointing": True,
    "optim": {"135m": "adamw", "360m": "adamw8bit"},
    # --- evaluation ---
    "eval_prompts": 500,      # prompts per eval set (dolly valid/test have <= this many)
    "n_samples": 8,           # samples per prompt for diversity, temperature 1.0, no top-k/top-p
    "sample_temperature": 1.0,
    "max_new_tokens": 256,
    "gen_batch": 64,          # sequences per generate() call for students (135M: ~23 KB KV per token)
    "teacher_gen_batch": 16,  # teacher: no GQA, ~0.2 MB of KV cache per token; 32 x 550 tokens OOMs on 8 GB
    "ece_bins": 15,
    "tail_ks": [8, 32, 128],  # tail coverage measured outside the teacher's top-k for these k
    "token_eval_examples": 300,  # held-out gold responses for token-level metrics
}

from .protocol import FROZEN_LR  # noqa: E402

if FROZEN_LR is not None:  # manual `cf.py train` without --lr then uses the frozen LR too
    CFG["lr"] = FROZEN_LR

ARMS = ("sft", "seqkd", "full", "topk", "tailbucket", "rs", "tailuniform")
DIVERGENCES = ("fkl", "rkl")


UNTAGGED_LR = 2e-4  # the pilot LR; any other LR is written into the run id, so ids never become ambiguous


def run_id(arm: str, div: str | None, k: int | None, student: str, n_train: int | None, seed: int,
           source: str = "cache", lr: float | None = None, ce_weight: float = 0.0, init: str | None = None) -> str:
    parts = [arm]
    if arm in ("topk", "tailbucket", "tailuniform"):
        parts.append(f"k{k}")
    if arm in ("full", "topk", "tailbucket", "rs", "tailuniform"):
        parts.append(div)
        if arm != "full" and source == "online":
            parts.append("online")
    if lr is not None and abs(lr - UNTAGGED_LR) > 1e-12:
        parts.append(f"lr{lr:.6g}")  # round-trips every LR we use; 2.5e-4 and 3e-4 stay distinct
    if ce_weight:
        parts.append(f"ce{ce_weight:.6g}")
    if init:
        parts.append(f"{init}init")  # e.g. "sftinit": started from the same-seed SFT student
    parts += [student, f"n{n_train or 'all'}", f"s{seed}", CONFIG_VERSION]
    return "_".join(parts)
