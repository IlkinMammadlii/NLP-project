"""Train one student arm. Every arm shares data order, steps, LR and schedule; only the target differs."""
import math
import random

import torch

from . import data as D
from .config import CFG, CONFIG_VERSION, run_id
from .losses import ce, kd_loss
from .models import check_shared_tokenizer, load_model, load_tokenizer, response_logits
from .targets import Cache, from_logits
from .utils import (Timer, append_jsonl, device, git_state, gpu_info, log, path, peak_mem_gb,
                    read_jsonl, reset_peak_mem, root, set_seed)


def done_runs() -> set:
    """Runs logged as finished whose checkpoint is on disk (a save=False smoke run never counts)."""
    return {r["run_id"] for r in read_jsonl(path("runs", "results.jsonl"))
            if r.get("saved") and (root() / "ckpt" / r["run_id"] / "config.json").exists()}


def make_optimizer(model, student: str, lr: float):
    params = [p for p in model.parameters() if p.requires_grad]
    decay = [p for p in params if p.ndim >= 2]
    no_decay = [p for p in params if p.ndim < 2]
    groups = [{"params": decay, "weight_decay": CFG["weight_decay"]}, {"params": no_decay, "weight_decay": 0.0}]
    if CFG["optim"].get(student, "adamw") == "adamw8bit":
        import bitsandbytes as bnb
        return bnb.optim.AdamW8bit(groups, lr=lr)
    return torch.optim.AdamW(groups, lr=lr)


def lr_lambda(total: int):
    warm = max(1, int(CFG["warmup_frac"] * total))
    return lambda s: (s + 1) / warm if s < warm else max(0.0, (total - s) / max(1, total - warm))


def train(arm: str, div: str | None = None, k: int | None = None, student: str = "135m",
          n_train: int | None = None, seed: int = 1, source: str = "cache", save: bool = True,
          lr: float | None = None, ce_weight: float | None = None, init_from: str | None = None) -> str:
    """One arm. `init_from` = run id of a trained student to start from (the "MiniLLM-style init"
    ablation starts KD from the same-seed SFT student); default: the base checkpoint."""
    assert arm in ("sft", "seqkd", "full", "topk", "tailbucket", "rs", "tailuniform"), arm
    if arm == "full":
        source = "online"
    lr = CFG["lr"] if lr is None else lr
    ce_weight = CFG["ce_weight"] if ce_weight is None else ce_weight
    assert not (ce_weight and arm in ("sft", "seqkd")), "ce_weight applies to KD arms only"
    assert init_from is None or init_from.startswith("sft_"), "only SFT initialisation is defined"
    rid = run_id(arm, div, k, student, n_train, seed, source, lr, ce_weight, "sft" if init_from else None)
    if rid in done_runs():
        log(f"skip {rid}: already in results.jsonl")
        return rid
    logf = path("logs", f"{rid}.log")
    set_seed(seed)
    dev = device()
    tok = load_tokenizer(student)
    rows = D.load_split("train", n_train)
    if arm == "seqkd":
        gen = {r["id"]: r["ids"] for r in read_jsonl(path("teacher", "seqkd_train.jsonl"))}
        missing = [r["id"] for r in rows if r["id"] not in gen]
        assert not missing, f"{len(missing)} train examples lack a teacher response: run `cf.py seqkd`"
        encs = [D.encode(r, tok, response_ids=gen[r["id"]]) for r in rows]
    else:
        encs = [D.encode(r, tok) for r in rows]
    ex_ids = [r["id"] for r in rows]

    teacher, cache = None, None
    if arm in ("full", "topk", "tailbucket", "rs", "tailuniform"):
        if source == "online":
            check_shared_tokenizer(tok, load_tokenizer("teacher"))
            teacher = load_model("teacher")
        else:
            cache = Cache("train")
            cache.verify(ex_ids, encs)

    if init_from:
        init_ck = root() / "ckpt" / init_from
        assert (init_ck / "config.json").exists(), f"no checkpoint {init_from} to initialise from"
    # fp32 master weights, bf16 autocast (an SFT checkpoint is stored in bf16 and upcast here)
    model = load_model(str(init_ck) if init_from else student, dtype=torch.float32, train=True)
    if CFG["grad_checkpointing"]:
        model.gradient_checkpointing_enable()
        model.config.use_cache = False
    opt = make_optimizer(model, student, lr)
    micro = CFG["micro_batch_online"] if teacher is not None else CFG["micro_batch"]
    accum = CFG["batch_size"] // micro
    steps_per_epoch = math.ceil(len(encs) / CFG["batch_size"])
    total = steps_per_epoch * CFG["epochs"]
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lr_lambda(total))
    gen = torch.Generator(device=dev).manual_seed(seed)  # RS draws in online mode
    order_rng = random.Random(seed)

    log(f"train {rid}: {len(encs)} examples, {total} steps, accum {accum}", logf)
    reset_peak_mem()
    step, losses = 0, []
    with Timer() as tm:
        for ep in range(CFG["epochs"]):
            order = list(range(len(encs)))
            order_rng.shuffle(order)
            for b0 in range(0, len(order), CFG["batch_size"]):
                batch_idx = order[b0:b0 + CFG["batch_size"]]
                n_tok = sum(len(encs[i]["resp"]) for i in batch_idx)
                step_loss = 0.0
                for m0 in range(0, len(batch_idx), micro):
                    mi = batch_idx[m0:m0 + micro]
                    loss = micro_step(model, teacher, cache, [encs[i] for i in mi], [ex_ids[i] for i in mi],
                                      arm, div, k, tok, gen, dev, ce_weight)
                    (loss / n_tok).backward()  # token-mean over the whole optimizer batch
                    step_loss += loss.item() / n_tok
                torch.nn.utils.clip_grad_norm_(model.parameters(), CFG["grad_clip"])
                opt.step()
                sched.step()
                opt.zero_grad(set_to_none=True)
                losses.append(step_loss)
                step += 1
                if step % 20 == 0 or step == total:
                    log(f"{rid} step {step}/{total} loss {sum(losses[-20:]) / len(losses[-20:]):.4f} "
                        f"lr {sched.get_last_lr()[0]:.2e} peak {peak_mem_gb()} GB", logf)
                if not math.isfinite(step_loss):
                    raise FloatingPointError(f"{rid}: non-finite loss at step {step}")
    if save:
        out = path("ckpt", rid, "x").parent
        model.generation_config.eos_token_id = [D.end_id(tok), tok.eos_token_id]
        model.generation_config.pad_token_id = tok.pad_token_id
        model.to(torch.bfloat16).save_pretrained(out)
    rec = {"run_id": rid, "arm": arm, "div": div, "k": k, "student": student, "n_train": len(encs),
           "seed": seed, "source": source if arm not in ("sft", "seqkd") else None, "steps": total, "saved": save, "init_from": init_from,
           "lr": lr, "ce_weight": ce_weight, "micro_batch": micro,
           "final_loss": sum(losses[-20:]) / max(1, len(losses[-20:])), "train_s": tm.s,
           "s_per_step": round(tm.s / max(1, total), 3), "peak_gpu_mem_gb": peak_mem_gb(),
           "config_version": CONFIG_VERSION, "cfg": {k_: v for k_, v in CFG.items()},
           "data_md5": read_jsonl(path("data", "data_info.jsonl"))[0]["train"]["md5"],
           **git_state(), **gpu_info()}
    append_jsonl(path("runs", "results.jsonl"), rec)
    log(f"done {rid} in {tm.s}s, peak {rec['peak_gpu_mem_gb']} GB", logf)
    del model, teacher
    torch.cuda.empty_cache()
    return rid


def micro_step(model, teacher, cache, encs, ex_ids, arm, div, k, tok, gen, dev, ce_weight) -> torch.Tensor:
    """Summed (not averaged) loss over the response tokens of one micro-batch."""
    sb = D.collate(encs, tok.pad_token_id, "student")
    gold = sb["targets"].to(dev)
    with torch.autocast(device_type=dev.type, dtype=torch.bfloat16, enabled=dev.type == "cuda"):
        s_logits = response_logits(model, sb)
    if arm in ("sft", "seqkd"):
        return ce(s_logits, gold).sum()
    if teacher is not None:
        tb = D.collate(encs, tok.pad_token_id, "teacher")
        assert torch.equal(tb["targets"], sb["targets"]), "teacher/student response positions misaligned"
        with torch.no_grad():
            t_logits = response_logits(teacher, tb)
        tgt = from_logits(t_logits, arm, k, CFG["rs_samples"], gen, cache_k=CFG["cache_k"])
    else:
        tgt = cache.targets(ex_ids, arm, k, dev)
        assert tgt.ids is None or tgt.ids.shape[0] == s_logits.shape[0], "cache rows != response positions"
        assert tgt.rs_ids is None or tgt.rs_ids.shape[0] == s_logits.shape[0], "cache rows != response positions"
    loss = kd_loss(s_logits, tgt, arm, div)
    if ce_weight:
        loss = loss + ce_weight * ce(s_logits, gold)
    return loss.sum()
