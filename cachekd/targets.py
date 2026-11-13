"""Teacher signals at response positions, built online from full teacher logits or read from the cache.

One `Targets` object feeds every loss, so an arm trained from the cache and the same arm trained
online differ only by fp16 rounding of the stored probabilities (checked in tests).
"""
import hashlib
import json
from dataclasses import dataclass

import numpy as np
import torch

from .utils import path


@dataclass
class Targets:
    """Teacher information for N response positions (N = rows of the student logits)."""
    logits: torch.Tensor | None = None    # (N, V) full teacher logits (online full KD only)
    ids: torch.Tensor | None = None       # (N, k) top-k token ids, sorted by probability
    probs: torch.Tensor | None = None     # (N, k) teacher probabilities of those ids (NOT renormalised)
    tail: torch.Tensor | None = None      # (N,) teacher mass outside the top-k, 1 - sum(probs)
    rs_ids: torch.Tensor | None = None    # (N, S) Random Sampling KD draws
    rs_w: torch.Tensor | None = None      # (N, S) weights per draw, each row sums to 1


def topk_tail(logits: torch.Tensor, k: int) -> tuple:
    """Top-k ids/probs and the exact leftover mass (computed in log space, no 1 - sum cancellation)."""
    lf = logits.float()
    lse = torch.logsumexp(lf, -1)
    top_l, ids = lf.topk(k, -1)
    probs = (top_l - lse[:, None]).exp()
    rest = lf.scatter(1, ids, float("-inf"))
    tail = (torch.logsumexp(rest, -1) - lse).exp()
    return ids, probs, tail


def rs_draws(logits: torch.Tensor, n: int, generator: torch.Generator | None = None) -> tuple:
    """Anshumann et al. (2025) Random Sampling KD with proposal = teacher (tau = 1): n draws with
    replacement, each with weight 1/n, so the estimate is count_i / n (unbiased for p_i)."""
    p = torch.softmax(logits.float(), -1)
    ids = torch.multinomial(p, n, replacement=True, generator=generator)
    w = torch.full(ids.shape, 1.0 / n, device=ids.device)
    return ids, w


def from_logits(logits: torch.Tensor, arm: str, k: int | None, rs_samples: int,
                generator: torch.Generator | None = None, cache_k: int | None = None) -> Targets:
    """Online targets. Top-k goes through the same top-`cache_k` + slice path as the cache, so that
    ties at the rank-k boundary are broken identically online and from the cache."""
    if arm == "full":
        return Targets(logits=logits)
    if arm in ("topk", "tailbucket", "tailuniform"):
        K = max(k, cache_k or k)
        ids, probs, tail = topk_tail(logits, K)
        return slice_topk(ids, probs, tail, k)
    if arm == "rs":
        ids, w = rs_draws(logits, rs_samples, generator)
        return Targets(rs_ids=ids, rs_w=w)
    raise ValueError(arm)


def slice_topk(ids128: torch.Tensor, probs128: torch.Tensor, tail128: torch.Tensor, k: int) -> Targets:
    """Top-k targets from the stored top-K: tail_k = tail_K + mass of ranks k+1..K."""
    return Targets(ids=ids128[:, :k], probs=probs128[:, :k],
                   tail=tail128 + probs128[:, k:].sum(-1))


# ---------------------------------------------------------------- disk cache
class Cache:
    """Memory-mapped teacher cache over the training split.

    Files (in $CF_ROOT/cache/<name>/): ids.npy (P, K) int32, probs.npy (P, K) float32,
    tail.npy (P,) float32, ent.npy (P,) float32 teacher entropy, rs_ids.npy (P, S) int32,
    index.json {example_id: [offset, length, token_hash]} and meta.json.
    Probabilities are fp32 (not the brief's fp16): fp16 flushes p < 6e-8 to 0, which matters for RKL
    and for tail sums, and fp32 costs only ~0.5 GB more here.
    """

    def __init__(self, name: str = "train"):
        d = path("cache", name, "x").parent
        self.dir = d
        self.meta = json.loads((d / "meta.json").read_text())
        self.index = json.loads((d / "index.json").read_text())
        self.ids = np.load(d / "ids.npy", mmap_mode="r")
        self.probs = np.load(d / "probs.npy", mmap_mode="r")
        self.tail = np.load(d / "tail.npy", mmap_mode="r")
        self.rs_ids = np.load(d / "rs_ids.npy", mmap_mode="r")

    def verify(self, ex_ids: list, encs: list) -> None:
        """Fail if the cached targets were built from different token ids (prompt format, truncation,
        tokenizer...). Row counts alone would not catch a same-length change."""
        bad = [i for i, e in zip(ex_ids, encs) if self.index[i][2] != token_hash(e)]
        assert not bad, f"cache built from different inputs for {len(bad)} examples (e.g. {bad[:3]}): rebuild it"

    def rows(self, ex_ids: list) -> np.ndarray:
        return np.concatenate([np.arange(o, o + n) for o, n, _ in (self.index[i] for i in ex_ids)])

    def targets(self, ex_ids: list, arm: str, k: int | None, dev: torch.device) -> Targets:
        r = self.rows(ex_ids)
        if arm in ("topk", "tailbucket", "tailuniform"):
            K = self.meta["k"]
            assert k <= K, f"k={k} > cached K={K}"
            ids = torch.from_numpy(np.asarray(self.ids[r])).long().to(dev)
            probs = torch.from_numpy(np.asarray(self.probs[r])).float().to(dev)
            tail = torch.from_numpy(np.asarray(self.tail[r])).float().to(dev)
            return slice_topk(ids, probs, tail, k)
        if arm == "rs":
            ids = torch.from_numpy(np.asarray(self.rs_ids[r])).long().to(dev)
            return Targets(rs_ids=ids, rs_w=torch.full(ids.shape, 1.0 / ids.shape[1], device=dev))
        raise ValueError(f"arm {arm} does not read the cache")


def token_hash(enc: dict) -> str:
    """Identity of one cached example: the exact teacher input ids and response ids."""
    return hashlib.md5(json.dumps([enc["t_prompt"], enc["resp"]]).encode()).hexdigest()[:12]
