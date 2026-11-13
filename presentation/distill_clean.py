"""Knowledge distillation with a cached teacher: one clean, self-contained training script.

This is a readable single-file version of the training code behind the report. Its losses give the
same values and gradients as the full experiment pipeline, which adds a memory-mapped cache on disk,
resumable runs, logging, checks and evaluation.

    teacher  = SmolLM2-1.7B-Instruct   (frozen, only gives probabilities)
    student  = SmolLM2-135M            (trained)
    data     = Dolly-15k instruction/response pairs

For every token of every training answer the teacher gives a probability distribution over the
whole vocabulary (49,152 tokens). The student is trained to match it. The four "arms" differ only
in how much of that distribution the student is allowed to see:

    full        the whole teacher distribution, teacher run live            (the reference)
    topk        only the teacher's k most likely tokens, renormalised       (the common cache)
    tailbucket  top-k plus ONE extra number: the total mass of all the rest (prior work: ghost token)
    rs          50 random draws from the teacher distribution               (Random-Sampling KD)

Usage (needs torch, transformers and datasets; the defaults are a small demo that fits an 8 GB GPU):
    python distill_clean.py --arm topk --k 8
    python distill_clean.py --arm rs --n_train 200 --epochs 1
"""
import argparse
import math
import random

import torch
import torch.nn.functional as F
from transformers import AutoModelForCausalLM, AutoTokenizer

TEACHER = "HuggingFaceTB/SmolLM2-1.7B-Instruct"
STUDENT = "HuggingFaceTB/SmolLM2-135M"
END = "<|im_end|>"  # end-of-answer token, the same id in both tokenizers

# The student sees the Alpaca / MiniLLM prompt (it is a base model and has no chat format) ...
STUDENT_PROMPT = ("Below is an instruction that describes a task. Write a response that appropriately "
                  "completes the request.\n\n### Instruction:\n{instruction}\n\n### Response:\n")
STUDENT_PROMPT_CTX = ("Below is an instruction that describes a task, paired with an input that provides "
                      "further context. Write a response that appropriately completes the request."
                      "\n\n### Instruction:\n{instruction}\n\n### Input:\n{context}\n\n### Response:\n")


# =============================================================== 1. data: two prompts, ONE answer
def encode(ex: dict, tok, max_len: int = 512) -> dict:
    """Token ids for one example.

    Teacher and student get different prompts (each its own format) but the SAME answer token ids.
    So the j-th answer position of the student and of the teacher predict the same token, which is
    what lets us compare their distributions position by position.
    """
    ctx = (ex.get("context") or "").strip()
    s_text = (STUDENT_PROMPT_CTX if ctx else STUDENT_PROMPT).format(instruction=ex["instruction"].strip(), context=ctx)
    user = ex["instruction"].strip() + (f"\n\n{ctx}" if ctx else "")
    t_text = tok.apply_chat_template([{"role": "user", "content": user}], tokenize=False, add_generation_prompt=True)
    def ids(text):
        return tok(text, add_special_tokens=False)["input_ids"]

    s_prompt = ids(s_text)
    resp = ids(ex["response"].strip()) + [tok.convert_tokens_to_ids(END)]
    # student prompt + answer is cut at max_len (as in the report); a cut answer loses its end token
    return {"s_prompt": s_prompt, "t_prompt": ids(t_text), "resp": resp[:max(1, max_len - len(s_prompt))]}


def collate(encs: list, pad_id: int, who: str) -> dict:
    """Right-padded batch plus the flat (row, position) of every answer prediction.

    logits[row, pos] predicts answer token j, so pos = len(prompt) - 1 + j (a causal LM predicts
    the NEXT token). Only these positions are scored: prompt tokens are never part of the loss.
    """
    key = "s_prompt" if who == "student" else "t_prompt"
    seqs = [e[key] + e["resp"] for e in encs]
    L = max(map(len, seqs))
    ids = torch.full((len(seqs), L), pad_id)
    mask = torch.zeros((len(seqs), L), dtype=torch.long)
    rows, pos = [], []
    for b, (e, s) in enumerate(zip(encs, seqs)):
        ids[b, :len(s)] = torch.tensor(s)
        mask[b, :len(s)] = 1
        rows += [b] * len(e["resp"])
        pos += [len(e[key]) - 1 + j for j in range(len(e["resp"]))]
    return {"input_ids": ids, "attention_mask": mask, "rows": torch.tensor(rows), "pos": torch.tensor(pos)}


def answer_logits(model, batch: dict) -> torch.Tensor:
    """(N, V) logits at the N answer positions of the batch.

    The final projection to the vocabulary is done in fp32: bf16 logits near 20 are rounded in
    steps of about 0.1, which would blur small probability differences, including the tail.
    """
    dev = next(model.parameters()).device
    h = model.get_decoder()(input_ids=batch["input_ids"].to(dev), attention_mask=batch["attention_mask"].to(dev)).last_hidden_state
    h = h[batch["rows"].to(dev), batch["pos"].to(dev)]
    W = model.get_output_embeddings().weight
    with torch.autocast(device_type=dev.type, enabled=False):
        return F.linear(h.float(), W.float())


# =============================================================== 2. the teacher pass (the "cache")
@torch.no_grad()
def build_cache(teacher, encs: list, tok, K: int, n_draws: int, gen: torch.Generator) -> list:
    """Run the teacher ONCE over the training answers and keep only a small summary per position.

    Storing all 49,152 probabilities for each of the report's 870,417 training positions would take
    171 GB.
    We keep instead, per position:
        ids  (K,)  the teacher's K most likely tokens          (K = 128; smaller k is a slice)
        p    (K,)  their probabilities (not renormalised)
        tail ()    1 - sum(p): the mass of everything else, computed exactly in log space
        rs   (S,)  S = 50 tokens drawn at random from the full teacher distribution
    """
    cache = []
    for e in encs:
        logits = answer_logits(teacher, collate([e], tok.pad_token_id, "teacher"))
        lse = torch.logsumexp(logits, -1)
        top_l, ids = logits.topk(K, -1)
        rest = logits.scatter(1, ids, float("-inf"))           # mask the top-K, keep the others
        cache.append({"ids": ids.cpu(), "p": (top_l - lse[:, None]).exp().cpu(),
                      "tail": (torch.logsumexp(rest, -1) - lse).exp().cpu(),
                      "rs": torch.multinomial(logits.softmax(-1), n_draws, replacement=True, generator=gen).cpu()})
    return cache


def cached_targets(cache: list, idx: list, k: int, dev) -> dict:
    """Targets for a micro-batch, in the same flat order as `collate`. Top-k is a slice of the top-K;
    the mass of ranks k+1..K moves into the tail."""
    def cat(key):
        return torch.cat([cache[i][key] for i in idx]).to(dev)

    ids, p, tail = cat("ids"), cat("p"), cat("tail")
    return {"ids": ids[:, :k], "p": p[:, :k], "tail": tail + p[:, k:].sum(-1), "rs": cat("rs")}


# =============================================================== 3. the four losses (forward KL)
# p = teacher, q = student. Every function returns one loss per position, in nats.
# KL(p || q) = sum_i p_i (log p_i - log q_i): zero when the student copies the teacher exactly, and
# large when the student gives low probability to a token the teacher likes ("mass-covering").

def xlogx(x):
    return torch.special.xlogy(x, x)  # x log x, with 0 log 0 = 0


def loss_full(s_logits, t_logits):
    """KL(p || q) over all 49,152 tokens: the student sees everything, including the tail."""
    lq, lp = F.log_softmax(s_logits, -1), F.log_softmax(t_logits, -1)
    return (lp.exp() * (lp - lq)).sum(-1)


def loss_topk(s_logits, t):
    """KL(p_T / p(T) || q): the target keeps only the top-k tokens, renormalised to sum to 1.

    The student keeps its full softmax, so every token outside the top-k has target 0: the loss
    pushes the student's mass there toward 0. This is the "sharpening" we measure.
    """
    phat = t["p"] / t["p"].sum(-1, keepdim=True)
    lq_k = F.log_softmax(s_logits, -1).gather(1, t["ids"])
    return xlogx(phat).sum(-1) - (phat * lq_k).sum(-1)


def loss_tailbucket(s_logits, t):
    """(k+1)-way KL: the top-k tokens plus ONE bucket that holds all other tokens together.

    Teacher: (p_1 .. p_k, tail).  Student: (q_1 .. q_k, 1 - q(top-k)).
    The student now keeps the right AMOUNT of tail mass, but nothing says WHICH tail tokens get it.
    """
    lq = F.log_softmax(s_logits, -1)
    lq_tail = torch.logsumexp(s_logits.scatter(1, t["ids"], float("-inf")), -1) - torch.logsumexp(s_logits, -1)
    lq = torch.cat([lq.gather(1, t["ids"]), lq_tail[:, None]], -1)
    p = torch.cat([t["p"], t["tail"][:, None]], -1)
    p = p / p.sum(-1, keepdim=True)
    return xlogx(p).sum(-1) - (p * lq).sum(-1)


def loss_rs(s_logits, t):
    """Random-Sampling KD: the target is p^_i = (times token i was drawn) / S.

    p^ is an unbiased estimate of the full teacher distribution (in expectation over the random
    draws), so tail tokens show up with their real identity, in proportion to their probability.
    The draws are made once, in the cache, and reused every epoch. In the report's cache the 50
    draws held 8.74 different tokens per position on average.
    """
    S = t["rs"].shape[1]
    w = torch.full(t["rs"].shape, 1.0 / S, device=s_logits.device)
    with torch.no_grad():  # sum p^ log p^ (minus the entropy of p^): a constant, so the printed
        # number is KL(p^ || q); it has no gradient
        ent = xlogx(torch.zeros_like(s_logits).scatter_add_(1, t["rs"], w)).sum(-1)
    return ent - (w * F.log_softmax(s_logits, -1).gather(1, t["rs"])).sum(-1)


# =============================================================== 4. training
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["full", "topk", "tailbucket", "rs"], default="topk")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--n_train", type=int, default=64, help="number of training examples (11,221 in the report)")
    ap.add_argument("--epochs", type=int, default=1, help="3 in the report")
    ap.add_argument("--lr", type=float, default=2e-4)  # frozen from the LR sweep
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    assert 1 <= args.k <= 128, "k must be between 1 and the cached K = 128"
    assert args.n_train > 0 and args.epochs > 0

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    batch, micro = 16, 2  # optimizer batch 16, built from micro-batches of 2 (gradient accumulation)

    tok = AutoTokenizer.from_pretrained(TEACHER)  # same vocabulary as the student; it has the chat template
    assert tok.get_vocab() == AutoTokenizer.from_pretrained(STUDENT).get_vocab(), "KD needs one vocabulary"
    if tok.pad_token_id is None:
        tok.pad_token = "<|endoftext|>"

    # Dolly-15k from the Hugging Face Hub. The report's experiments use a filtered split of it (long
    # prompts dropped, test/validation overlap removed); for this demo the raw data is enough.
    from datasets import load_dataset
    rows = list(load_dataset("databricks/databricks-dolly-15k", split="train"))
    encs = [encode(r, tok) for r in rows[:args.n_train]]

    # ---- teacher: live for full KD, otherwise one pass that is summarised into the cache, then freed
    teacher = AutoModelForCausalLM.from_pretrained(TEACHER, dtype=torch.bfloat16).to(dev).eval().requires_grad_(False)
    cache = None
    if args.arm != "full":  # every cached arm reads its own slice of the same cache
        cache = build_cache(teacher, encs, tok, K=128, n_draws=50, gen=torch.Generator(device=dev).manual_seed(args.seed))
        del teacher
        teacher = None
        torch.cuda.empty_cache()

    # ---- student: fp32 master weights, bf16 compute (autocast), AdamW, 5% warm-up then linear decay
    student = AutoModelForCausalLM.from_pretrained(STUDENT, dtype=torch.float32).to(dev).train()
    params = [p for p in student.parameters() if p.requires_grad]
    opt = torch.optim.AdamW([{"params": [p for p in params if p.ndim >= 2], "weight_decay": 0.01},
                             {"params": [p for p in params if p.ndim < 2], "weight_decay": 0.0}], lr=args.lr)
    total = math.ceil(len(encs) / batch) * args.epochs
    warm = max(1, int(0.05 * total))

    def lr_factor(s):
        return (s + 1) / warm if s < warm else max(0.0, (total - s) / max(1, total - warm))

    sched = torch.optim.lr_scheduler.LambdaLR(opt, lr_factor)

    step = 0
    for ep in range(args.epochs):
        order = list(range(len(encs)))
        random.shuffle(order)  # the same order for every arm (same seed): only the target differs
        for b0 in range(0, len(order), batch):
            idx = order[b0:b0 + batch]
            n_tok = sum(len(encs[i]["resp"]) for i in idx)
            step_loss = 0.0
            for m0 in range(0, len(idx), micro):
                mi = idx[m0:m0 + micro]
                with torch.autocast(device_type=dev.type, dtype=torch.bfloat16, enabled=dev.type == "cuda"):
                    s_logits = answer_logits(student, collate([encs[i] for i in mi], tok.pad_token_id, "student"))

                if args.arm == "full":
                    with torch.no_grad():
                        t_logits = answer_logits(teacher, collate([encs[i] for i in mi], tok.pad_token_id, "teacher"))
                    loss = loss_full(s_logits, t_logits)
                else:
                    t = cached_targets(cache, mi, args.k, dev)
                    loss = {"topk": loss_topk, "tailbucket": loss_tailbucket, "rs": loss_rs}[args.arm](s_logits, t)

                # pure KD (no cross-entropy on the gold token); mean over ALL answer tokens of the
                # optimizer batch, so long and short answers count per token
                (loss.sum() / n_tok).backward()
                step_loss += loss.sum().item() / n_tok

            torch.nn.utils.clip_grad_norm_(student.parameters(), 1.0)
            opt.step()
            sched.step()
            opt.zero_grad(set_to_none=True)
            step += 1
            # (the RS loss is measured against the 50-draw estimate p^, so its value is not
            # directly comparable with the other arms)
            print(f"epoch {ep + 1} step {step}/{total}  KL {step_loss:.4f}  next lr {sched.get_last_lr()[0]:.2e}")

    # ---- a quick look at what the student writes now (greedy, the student's own prompt format)
    student.eval()
    ex = {"instruction": "Give me three tips for staying focused while studying.", "context": ""}
    prompt = torch.tensor([encode({**ex, "response": ""}, tok)["s_prompt"]], device=dev)
    with torch.no_grad():
        out = student.generate(prompt, max_new_tokens=80, do_sample=False,
                               eos_token_id=tok.convert_tokens_to_ids(END), pad_token_id=tok.pad_token_id)
    print("\n" + ex["instruction"] + "\n->", tok.decode(out[0, prompt.shape[1]:], skip_special_tokens=True))


if __name__ == "__main__":
    main()
