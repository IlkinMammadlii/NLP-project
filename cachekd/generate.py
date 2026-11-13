"""Batched generation and sequence scoring shared by the teacher phases and evaluation."""
import torch
import torch.nn.functional as F
from transformers import GenerationConfig

from . import data as D
from .models import response_logits


def gen_config(tok, greedy: bool, temperature: float, max_new: int, n: int = 1,
               static: bool = False) -> GenerationConfig:
    """Explicit config: pure ancestral sampling (no top-k / top-p / repetition penalty), so measured
    diversity is the model's own and not a decoding default (HF defaults to top_k=50).

    static=True preallocates the KV cache (used for the 1.7B teacher, which has no GQA): the default
    dynamic cache is re-concatenated every step and drove PyTorch's reserved memory to 10.6 GB on the
    8 GB card, so Windows spilled to system RAM (3x slower). Same greedy output (checked 17 Oct);
    torch.compile is off (no Triton on Windows)."""
    eos = [D.end_id(tok)] + ([tok.eos_token_id] if tok.eos_token_id is not None else [])
    kw = dict(max_new_tokens=max_new, eos_token_id=sorted(set(eos)), pad_token_id=tok.pad_token_id,
              num_return_sequences=n, repetition_penalty=1.0)
    if static:
        kw.update(cache_implementation="static", disable_compile=True)
    if greedy:
        return GenerationConfig(do_sample=False, num_beams=1, **kw)
    return GenerationConfig(do_sample=True, temperature=temperature, top_k=0, top_p=1.0, **kw)


@torch.no_grad()
def generate(model, tok, prompts: list, greedy: bool, n: int, max_new: int, batch: int,
             temperature: float = 1.0, seed: int = 0, static: bool = False) -> list:
    """For each prompt (list of ids) return n generations as dicts {ids, text, ended}."""
    dev = next(model.parameters()).device
    cfg = gen_config(tok, greedy, temperature, max_new, 1 if greedy else n, static)
    eos = set(cfg.eos_token_id)
    per_call = max(1, batch // (1 if greedy else n))
    torch.manual_seed(seed)
    out = []
    for i in range(0, len(prompts), per_call):
        new = _generate_oom_safe(model, tok, prompts[i:i + per_call], cfg, dev)
        if dev.type == "cuda":
            torch.cuda.empty_cache()  # varying shapes otherwise fragment the cache past the 8 GB card
        reps = 1 if greedy else n
        for j in range(0, len(new), reps):
            group = []
            for s in new[j:j + reps]:
                cut = next((t for t, x in enumerate(s) if x in eos), None)
                toks = s if cut is None else s[:cut + 1]
                group.append({"ids": toks, "ended": cut is not None,
                              "text": tok.decode(toks if cut is None else toks[:-1], skip_special_tokens=True)})
            out.append(group)
    return out


def _is_oom(e: Exception) -> bool:
    return isinstance(e, torch.OutOfMemoryError) or "out of memory" in str(e)


def _generate_oom_safe(model, tok, prompts: list, cfg, dev) -> list:
    """New token ids for `prompts`; on CUDA OOM (e.g. another app briefly using the GPU during a multi-day
    run) free the cache and retry the two halves. Greedy output is unchanged by the split; sampled output
    draws from the same seeded stream in a different batch layout (logged)."""
    ids, mask = D.left_pad(prompts, tok.pad_token_id)
    try:
        seqs = model.generate(input_ids=ids.to(dev), attention_mask=mask.to(dev), generation_config=cfg)
        return seqs[:, ids.shape[1]:].tolist()
    except Exception as e:
        if not _is_oom(e) or len(prompts) == 1:
            raise
    del ids, mask
    import gc
    gc.collect()
    torch.cuda.empty_cache()
    from .utils import log
    h = len(prompts) // 2
    log(f"generate: CUDA OOM with {len(prompts)} prompts, retrying as {h} + {len(prompts) - h}")
    return _generate_oom_safe(model, tok, prompts[:h], cfg, dev) + _generate_oom_safe(model, tok, prompts[h:], cfg, dev)


@torch.no_grad()
def mean_logprob(model, tok, prompts: list, responses: list, who: str, batch: int = 8) -> list:
    """Per-sequence mean token log-probability of `responses` (id lists) given `prompts` (id lists)."""
    key = "s_prompt" if who == "student" else "t_prompt"
    res = []
    dev = next(model.parameters()).device
    for i in range(0, len(prompts), batch):
        encs = [{key: p, "resp": r} for p, r in zip(prompts[i:i + batch], responses[i:i + batch]) if r]
        if not encs:
            res.extend([float("nan")] * len(prompts[i:i + batch]))
            continue
        b = D.collate(encs, tok.pad_token_id, who)
        lp = F.log_softmax(response_logits(model, b).float(), -1)
        tok_lp = lp.gather(1, b["targets"].to(dev)[:, None])[:, 0].cpu()
        sums = torch.zeros(len(encs)).index_add_(0, b["rows"], tok_lp)
        cnt = torch.zeros(len(encs)).index_add_(0, b["rows"], torch.ones_like(tok_lp))
        vals = iter((sums / cnt).tolist())
        res.extend(next(vals) if r else float("nan") for r in responses[i:i + batch])
    return res
