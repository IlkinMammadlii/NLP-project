"""Teacher phases: one pass to cache top-K / tail mass / RS draws, and seq-KD responses."""
import json

import numpy as np
import torch

from . import data as D
from .config import CFG, CONFIG_VERSION, MODELS
from .generate import generate
from .models import check_shared_tokenizer, head_logits, load_model, load_tokenizer, response_hidden
from .targets import rs_draws, token_hash, topk_tail
from .utils import (Timer, append_jsonl, gpu_info, log, md5_ids, path, peak_mem_gb, read_jsonl,
                    reset_peak_mem)


CHUNK = 512  # positions per full-vocabulary block (512 x 49,152 fp32 = 96 MB per tensor)


def build_cache(name: str = "train", n: int | None = None, seed: int = 0) -> None:
    """Teacher-forced pass over the gold responses of a split. Resumable (progress.json)."""
    d = path("cache", name, "x").parent
    if (d / "meta.json").exists():
        log(f"cache {name} already complete: {d}")
        return
    tok = load_tokenizer("135m")
    check_shared_tokenizer(tok, load_tokenizer("teacher"))
    rows = D.load_split(name, n)
    encs = [D.encode(r, tok) for r in rows]
    hashes = [token_hash(e) for e in encs]
    lens = [len(e["resp"]) for e in encs]
    offs = np.concatenate([[0], np.cumsum(lens)[:-1]]).tolist()
    P, K, S = int(sum(lens)), CFG["cache_k"], CFG["rs_samples"]
    bs = CFG["cache_batch"]
    # everything that determines the cache contents; a resume with a different manifest is refused
    manifest = {"inputs_md5": md5_ids(hashes), "k": K, "rs_samples": S, "seed": seed, "batch": bs,
                "teacher": MODELS["teacher"], "teacher_format": CFG["teacher_format"],
                "config_version": CONFIG_VERSION}
    files = {"ids": ((P, K), np.int32), "probs": ((P, K), np.float32), "tail": ((P,), np.float32),
             "ent": ((P,), np.float32), "rs_ids": ((P, S), np.int32)}
    prog_f = d / "progress.json"
    start = 0
    if prog_f.exists():
        prog = json.loads(prog_f.read_text())
        assert prog["manifest"] == manifest, f"partial cache was built with other settings: delete {d}"
        start = prog["next"]
    mm = {k: np.lib.format.open_memmap(d / f"{k}.npy", mode="r+" if start else "w+", dtype=dt, shape=sh)
          for k, (sh, dt) in files.items()}
    teacher = load_model("teacher")
    gen = torch.Generator(device=next(teacher.parameters()).device)
    order = sorted(range(len(encs)), key=lambda i: -len(encs[i]["t_prompt"]) - lens[i])  # length-sorted
    reset_peak_mem()
    uniq, done_pos = [], 0
    log(f"cache {name}: {len(encs)} examples, {P} positions, K={K}, S={S}, starting at example {start}")
    with Timer() as tm, torch.no_grad():
        for b0 in range(start, len(order), bs):
            idx = order[b0:b0 + bs]
            if b0 > start and b0 % (bs * 50) == 0:
                for m in mm.values():
                    m.flush()
                prog_f.write_text(json.dumps({"next": b0, "manifest": manifest}))
                log(f"cache {b0}/{len(order)} examples, peak {peak_mem_gb()} GB")
            gen.manual_seed(seed * 1_000_003 + b0)  # RS draws do not depend on where a resumed run starts
            batch = D.collate([encs[i] for i in idx], tok.pad_token_id, "teacher")
            hid = response_hidden(teacher, batch)
            out = {k_: [] for k_ in ("ids", "probs", "tail", "ent", "rs_ids")}
            for c0 in range(0, hid.shape[0], CHUNK):  # vocabulary-sized tensors only for CHUNK positions
                logits = head_logits(teacher, hid[c0:c0 + CHUNK])
                c_ids, c_probs, c_tail = topk_tail(logits, K)
                lp = torch.log_softmax(logits, -1)
                c_ent = -(lp.exp() * lp).sum(-1)
                del lp
                c_rs, _ = rs_draws(logits, S, gen)
                del logits
                srt = c_rs.sort(-1).values
                uniq.append(((srt[:, 1:] != srt[:, :-1]).sum(-1) + 1).float().cpu())
                for k_, v in zip(out, (c_ids, c_probs, c_tail, c_ent, c_rs)):
                    out[k_].append(v.cpu())
            ids, probs, tail, ent, rs_ids = (torch.cat(out[k_]).numpy() for k_ in out)
            r0 = 0
            for i in idx:  # scatter rows back to each example's slot
                n_i, o = lens[i], offs[i]
                mm["ids"][o:o + n_i] = ids[r0:r0 + n_i]
                mm["probs"][o:o + n_i] = probs[r0:r0 + n_i]
                mm["tail"][o:o + n_i] = tail[r0:r0 + n_i]
                mm["ent"][o:o + n_i] = ent[r0:r0 + n_i]
                mm["rs_ids"][o:o + n_i] = rs_ids[r0:r0 + n_i]
                r0 += n_i
            done_pos += r0
    for m in mm.values():
        m.flush()
    index = {r["id"]: [int(o), int(n_), h] for r, o, n_, h in zip(rows, offs, lens, hashes)}
    (d / "index.json").write_text(json.dumps(index))
    tail = np.asarray(mm["tail"])
    probs = mm["probs"]
    tail_k = {str(k): float((tail + np.asarray(probs[:, k:]).sum(-1)).mean()) for k in CFG["tail_ks"] if k < K}
    meta = {"split": name, "n_examples": len(rows), "positions": P, **manifest,
            "bytes": {k: int(np.asarray(m).nbytes) for k, m in mm.items()},
            "seconds_this_session": tm.s, "positions_this_session": done_pos,
            "positions_per_s": round(done_pos / max(tm.s, 1e-9), 1),
            "peak_gpu_mem_gb": peak_mem_gb(), "resumed_from_example": start,
            "rs_unique_mean": float(torch.cat(uniq).mean()) if uniq else None,
            "teacher_tail_mass_mean": tail_k | {str(K): float(tail.mean())},
            "teacher_entropy_mean": float(np.asarray(mm["ent"]).mean()), **gpu_info()}
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    prog_f.unlink(missing_ok=True)
    log(f"cache done: {json.dumps(meta)}")


def build_seqkd(n: int | None = None, batch: int | None = None) -> None:
    """Teacher greedy responses to the training prompts (seq-KD). Keeps the generated token ids and
    whether the teacher stopped, so truncated responses never get an invented end token. Resumable."""
    out = path("teacher", "seqkd_train.jsonl")
    have = {r["id"] for r in read_jsonl(out)}
    tok = load_tokenizer("135m")
    rows = [r for r in D.load_split("train", n) if r["id"] not in have]
    # similar prompt lengths per batch: less padding and fewer shapes (output is per example, order-free)
    rows.sort(key=lambda r: len(D.encode_ids(D.teacher_prompt(r, tok), tok)))
    if not rows:
        log("seqkd: nothing to do")
        return
    teacher = load_model("teacher")
    prompts = [D.encode_ids(D.teacher_prompt(r, tok), tok) for r in rows]
    chunk = 256
    for c0 in range(0, len(rows), chunk):
        with Timer() as tm:
            gens = generate(teacher, tok, prompts[c0:c0 + chunk], greedy=True, n=1,
                            max_new=CFG["max_new_tokens"], batch=batch or CFG["teacher_gen_batch"], static=True)
        for r, g in zip(rows[c0:c0 + chunk], gens):
            append_jsonl(out, {"id": r["id"], "response": g[0]["text"], "ids": g[0]["ids"],
                               "ended": g[0]["ended"]})
        log(f"seqkd {c0 + len(gens)}/{len(rows)} ({tm.s}s for {len(gens)})")
    rs = read_jsonl(out)
    log(f"seqkd: {len(rs)} responses, ended {np.mean([r['ended'] for r in rs]):.3f}, "
        f"mean tokens {np.mean([len(r['ids']) for r in rs]):.1f}")
