"""Evaluate one student (or the teacher) on a split: quality, diversity, tail coverage,
calibration and precision/recall against the teacher. Appends one row to runs/evals.jsonl.

"precision" / "recall" (brief's names) are cross log-likelihoods: the teacher's per-token log-likelihood
of the student's samples, and the student's per-token log-likelihood of the teacher's samples.
"""
import numpy as np
import torch
import torch.nn.functional as F

from . import data as D
from . import metrics as M
from .config import CFG, CONFIG_VERSION, MODELS
from .generate import generate, mean_logprob
from .models import load_model, load_tokenizer, response_logits
from .utils import Timer, append_jsonl, git_state, log, path, read_jsonl, root, write_jsonl

EVAL_SCHEMA = 2  # bump when metrics are added: student evals then rerun (teacher rows carry no token metrics)
TAIL_MIN = 1e-6  # ratio metrics skip positions where the teacher's tail mass is below this (count reported)
FREQ_EDGES = [10, 100, 1_000, 10_000]  # gold-token buckets by count in the training responses: <10, 10-99, ...


def train_token_counts(tok) -> np.ndarray:
    """How often each vocabulary id occurs in the gold responses of the FULL train split (a corpus
    property, also used for n=2k runs). Cached with the train-split hash; rebuilt if the data changed."""
    import json
    import os
    train_md5 = read_jsonl(path("data", "data_info.jsonl"))[0]["train"]["md5"]
    f, meta = path("data", "train_token_counts.npy"), path("data", "train_token_counts.json")
    want = {"train_md5": train_md5, "vocab": len(tok), "max_len": CFG["max_len"]}
    if f.exists() and meta.exists() and json.loads(meta.read_text()) == want:
        return np.load(f)
    counts = np.zeros(len(tok), dtype=np.int64)
    for r in D.load_split("train"):
        np.add.at(counts, D.encode(r, tok)["resp"], 1)
    tmp = path("data", "train_token_counts.tmp.npy")
    np.save(tmp, counts)
    os.replace(tmp, f)
    meta.write_text(json.dumps(want))
    return counts


def done_evals() -> set:
    return {eval_key(r["run_id"], r["split"], r["n_prompts"], r["seed"], r.get("config_version"), r.get("eval_schema"))
            for r in read_jsonl(path("runs", "evals.jsonl"))}


def eval_key(run, split, n, seed, version, schema) -> tuple:
    return (run, split, n, seed, version, None if run == "teacher" else schema)


def teacher_samples_file(split: str, n: int, seed: int):
    return path("teacher", f"samples_{split}_n{n}_s{seed}_{CONFIG_VERSION}.jsonl")


def model_ref(run: str) -> tuple:
    """'teacher', a base model key ('135m'), or a trained run id -> (load name, prompt side)."""
    if run == "teacher":
        return "teacher", "teacher"
    if run in MODELS:
        return run, "student"
    ck = root() / "ckpt" / run
    assert (ck / "config.json").exists(), f"no checkpoint for {run}"
    return str(ck), "student"


@torch.no_grad()
def token_metrics(model, teacher, tok, rows: list) -> dict:
    """Teacher-forced on held-out gold responses: entropy, KL, calibration, gold-token NLL for tokens the
    teacher ranks outside its top-8 and by training-frequency bucket (FREQ_EDGES), and per top-k set T:
    student mass outside T (q_out) vs teacher (p_out), tail coverage sum_{i not in T} min(p_i, q_i) / p_out
    (per position and mass-weighted), and the within-tail conditional KL( p(.|not T) || q(.|not T) ),
    which separates "right amount of tail mass" from "mass on the right tail tokens"."""
    ks = CFG["tail_ks"]
    acc = {}

    def put(k, v):
        acc.setdefault(k, []).append(v.double().cpu())

    counts = train_token_counts(tok)
    encs = [D.encode(r, tok) for r in rows]
    for i in range(0, len(encs), 4):
        sb = D.collate(encs[i:i + 4], tok.pad_token_id, "student")
        tb = D.collate(encs[i:i + 4], tok.pad_token_id, "teacher")
        lq = F.log_softmax(response_logits(model, sb), -1)
        lp = F.log_softmax(response_logits(teacher, tb), -1)
        q, p = lq.exp(), lp.exp()
        gold = sb["targets"].to(lq.device)
        put("H_q", -(q * lq).sum(-1))
        put("H_p", -(p * lp).sum(-1))
        put("fkl", (p * (lp - lq)).sum(-1))
        put("rkl", (q * (lq - lp)).sum(-1))
        put("top1_agree", (lq.argmax(-1) == lp.argmax(-1)).float())
        put("nll", -lq.gather(1, gold[:, None])[:, 0])
        conf, pred = q.max(-1)
        put("conf", conf)
        put("correct", (pred == gold).float())
        top = lp.topk(max(8, *ks), -1).indices  # same top-k procedure as the cache (no full sort)
        put("nll_t", -lp.gather(1, gold[:, None])[:, 0])
        put("gold_rank_lt8", (top[:, :8] == gold[:, None]).any(-1).float())  # gold inside the teacher's top-8
        put("gold_count", torch.as_tensor(counts[gold.cpu().numpy()], dtype=torch.float64))
        # gold tokens that also occur in the prompt (copyable): top-k keeps those easily, so the frequency
        # buckets are also reported without them
        in_prompt = [t in set(encs[i + r]["s_prompt"]) for r, t in zip(sb["rows"].tolist(), sb["targets"].tolist())]
        put("in_prompt", torch.tensor(in_prompt, dtype=torch.float64))
        plr = p * (lp - lq)
        for k in ks:
            out = torch.ones_like(p, dtype=torch.bool).scatter(1, top[:, :k], False)
            p_out, q_out = (p * out).sum(-1), (q * out).sum(-1)
            put(f"p_out{k}", p_out)
            put(f"q_out{k}", q_out)
            put(f"minpq_out{k}", (torch.minimum(p, q) * out).sum(-1))
            # KL(p(.|out) || q(.|out)) = sum_out p (lp - lq) / p_out - log p_out + log q_out
            po, qo = p_out.clamp_min(1e-30), q_out.clamp_min(1e-30)
            put(f"cond_kl{k}", (plr * out).sum(-1) / po - po.log() + qo.log())
    t = {k: torch.cat(v).numpy().astype(np.float64) for k, v in acc.items()}
    res = {f"tok_{k}": float(t[k].mean()) for k in ("H_q", "H_p", "fkl", "rkl", "top1_agree", "nll")}
    res["tok_ece"] = M.expected_calibration_error(t["conf"], t["correct"], CFG["ece_bins"])
    res["tok_acc"] = float(t["correct"].mean())
    res["tok_entropy_ratio"] = float(t["H_q"].mean() / t["H_p"].mean())
    res["tok_positions"] = int(len(t["nll"]))
    # gold tokens the teacher itself ranks outside its top-8: does the student keep mass on them?
    tail_gold = t["gold_rank_lt8"] == 0
    res["tok_goldtail8_frac"] = float(tail_gold.mean())
    if tail_gold.any():
        res["tok_goldtail8_nll_s"] = float(t["nll"][tail_gold].mean())
        res["tok_goldtail8_nll_t"] = float(t["nll_t"][tail_gold].mean())
    # fact-independent long tail: gold tokens bucketed by their frequency in the training responses
    # (nll_s / nll_t: gold-token NLL of student / teacher; fkl_fullvocab: full-vocabulary KL at those positions)
    b = np.searchsorted(FREQ_EDGES, t["gold_count"], side="right")
    for tag, keep in (("", np.ones_like(b, dtype=bool)), ("_nocopy", t["in_prompt"] == 0)):
        for i in range(len(FREQ_EDGES) + 1):
            m = (b == i) & keep
            res[f"tok_freqb{i}{tag}_n"] = int(m.sum())
            if m.any():
                res[f"tok_freqb{i}{tag}_nll_s"] = float(t["nll"][m].mean())
                res[f"tok_freqb{i}{tag}_nll_t"] = float(t["nll_t"][m].mean())
                res[f"tok_freqb{i}{tag}_fkl_fullvocab"] = float(t["fkl"][m].mean())
    for k in ks:
        p_out, q_out, mpq = t[f"p_out{k}"], t[f"q_out{k}"], t[f"minpq_out{k}"]
        ok = p_out >= TAIL_MIN
        res[f"tok_p_out_top{k}"] = float(p_out.mean())
        res[f"tok_q_out_top{k}"] = float(q_out.mean())
        res[f"tok_tail_mass_abs_err_top{k}"] = float(np.abs(q_out - p_out).mean())
        res[f"tok_tail_cov_top{k}"] = float((mpq[ok] / p_out[ok]).mean()) if ok.any() else float("nan")
        res[f"tok_tail_cov_mass_top{k}"] = float(mpq.sum() / max(p_out.sum(), 1e-30))
        res[f"tok_tail_cond_kl_top{k}"] = float(t[f"cond_kl{k}"][ok].mean()) if ok.any() else float("nan")
        res[f"tok_tail_positions_top{k}"] = int(ok.sum())
    return res


def evaluate(run: str, split: str = "valid", n_prompts: int | None = None, seed: int = 0,
             with_teacher: bool = True) -> dict:
    name, side = model_ref(run)
    tok = load_tokenizer("135m")
    rows = D.load_eval_set(split, n_prompts or CFG["eval_prompts"])
    if eval_key(run, split, len(rows), seed, CONFIG_VERSION, EVAL_SCHEMA) in done_evals():
        log(f"skip eval {run} {split} n={len(rows)} seed={seed}: done")
        return {}
    tg_f = teacher_samples_file(split, len(rows), seed)
    if with_teacher and side == "student":
        assert tg_f.exists(), (f"evaluate the teacher first (cf.py eval --run teacher --split {split} "
                               f"--n-prompts {len(rows)}): its samples are needed for the recall metric")
    prompts = [D.encode_ids(D.teacher_prompt(r, tok) if side == "teacher" else D.student_prompt(r), tok)
               for r in rows]
    model = load_model(name)
    res = {"run_id": run, "split": split, "n_prompts": len(rows), "seed": seed, "eval_schema": EVAL_SCHEMA}
    gb = CFG["teacher_gen_batch"] if side == "teacher" else CFG["gen_batch"]
    st = side == "teacher"  # static KV cache for the 1.7B teacher (see generate.gen_config)
    with Timer() as tg:
        greedy = generate(model, tok, prompts, greedy=True, n=1, max_new=CFG["max_new_tokens"], batch=gb, static=st)
        samples = generate(model, tok, prompts, greedy=False, n=CFG["n_samples"], max_new=CFG["max_new_tokens"],
                           batch=gb, temperature=CFG["sample_temperature"], seed=seed, static=st)
    res["gen_s"] = tg.s
    refs = [r["refs"] for r in rows]
    res["rougeL_greedy"] = M.corpus_rouge_l([g[0]["text"] for g in greedy], refs)
    sample_txt = [[s["text"] for s in g] for g in samples]
    res["rougeL_sampled"] = float(np.mean([np.mean([M.rouge_l(s, rf) for s in g]) for g, rf in zip(sample_txt, refs)]))
    res["distinct1"] = M.distinct_n_per_prompt(sample_txt, 1)
    res["distinct2"] = M.distinct_n_per_prompt(sample_txt, 2)
    # length-controlled: distinct-n falls with length, so also score only the first 32 words of each sample
    first32 = [[" ".join(M.normalize_answer(s).split()[:32]) for s in g] for g in sample_txt]
    res["distinct2_first32"] = M.distinct_n_per_prompt(first32, 2)
    res["distinct2_corpus_greedy"] = M.distinct_n([g[0]["text"] for g in greedy], 2)
    res["self_bleu"] = M.mean_self_bleu(sample_txt)
    res["ended_rate_sampled"] = float(np.mean([s["ended"] for g in samples for s in g]))
    res["empty_rate_sampled"] = float(np.mean([not s["text"].strip() for g in samples for s in g]))
    res["len_sampled"] = float(np.mean([len(s["ids"]) for g in samples for s in g]))
    res["len_greedy"] = float(np.mean([len(g[0]["ids"]) for g in greedy]))
    write_jsonl(path("gens", f"{run}__{split}__n{len(rows)}__s{seed}.jsonl"),
                [{"id": r["id"], "greedy": g[0]["text"], "samples": s, "samples_ended": [x["ended"] for x in sg]}
                 for r, g, s, sg in zip(rows, greedy, sample_txt, samples)])
    # semantic diversity over the non-empty samples of each prompt (empty rate reported above)
    from sentence_transformers import SentenceTransformer
    emb = SentenceTransformer(MODELS["embedder"], device=str(next(model.parameters()).device))
    flat = [s for g in sample_txt for s in g]
    vecs = emb.encode(flat, batch_size=128, convert_to_numpy=True, show_progress_bar=False)
    if hasattr(emb, "tokenizer") and getattr(emb, "max_seq_length", None):
        lens = [len(x) for x in emb.tokenizer(flat)["input_ids"]]
        res["embed_truncated_rate"] = float(np.mean([n > emb.max_seq_length for n in lens]))
    K = CFG["n_samples"]
    groups = [np.stack([v for v, s in zip(vecs[i * K:(i + 1) * K], g) if s.strip()] or [np.zeros(vecs.shape[1])])
              for i, g in enumerate(sample_txt)]
    res["semantic_div"] = M.mean_semantic_diversity(groups)
    del emb
    if with_teacher and side == "student":
        teacher = load_model("teacher")
        if split in ("valid", "test"):  # teacher-forced token metrics need our own gold splits
            res.update(token_metrics(model, teacher, tok, D.load_split(split, CFG["token_eval_examples"])))
        # precision: teacher log-likelihood of the student's samples (per token)
        t_prompts = [D.encode_ids(D.teacher_prompt(r, tok), tok) for r in rows]
        flat_p = [p for p in t_prompts for _ in range(K)]
        flat_r = [s["ids"] for g in samples for s in g]
        res["precision_teacher_lp"] = float(np.nanmean(mean_logprob(teacher, tok, flat_p, flat_r, "teacher")))
        # recall: student log-likelihood of the teacher's samples (same prompts, same sampling seed)
        t_samp = {r["id"]: r["sample_ids"] for r in read_jsonl(tg_f)}
        fr = [(p, s) for r, p in zip(rows, prompts) for s in t_samp[r["id"]]]
        res["recall_student_lp"] = float(np.nanmean(mean_logprob(model, tok, [a for a, _ in fr],
                                                                 [b for _, b in fr], "student")))
        del teacher
    if side == "teacher":  # keep the teacher's samples (ids) for the recall metric of every student
        write_jsonl(tg_f, [{"id": r["id"], "sample_ids": [s["ids"] for s in g]} for r, g in zip(rows, samples)])
    res.update({"config_version": CONFIG_VERSION, **git_state()})
    append_jsonl(path("runs", "evals.jsonl"), res)
    log(f"eval {run} {split}: " + ", ".join(f"{k}={v:.4g}" for k, v in res.items() if isinstance(v, float)))
    del model
    torch.cuda.empty_cache()
    return res
