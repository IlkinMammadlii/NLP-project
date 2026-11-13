"""End-to-end pipeline on CPU with tiny random Llama models and a word-level tokenizer (no downloads).
Run: python tests/test_pipeline.py   (must end with ALL PIPELINE TESTS PASSED)"""
import json
import os
import random
import sys
import tempfile
import types
from pathlib import Path

ROOT = tempfile.mkdtemp(prefix="cf_test_")
os.environ["CF_ROOT"] = ROOT
os.environ["CF_DEVICE"] = "cpu"

import numpy as np  # noqa: E402
import torch  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tokenizers import Tokenizer, models, pre_tokenizers, decoders  # noqa: E402
from transformers import LlamaConfig, LlamaForCausalLM, PreTrainedTokenizerFast  # noqa: E402

from cachekd import config, data as D  # noqa: E402
from cachekd.config import CFG, MODELS  # noqa: E402

# ------------------------------------------------------------------ tiny world
WORDS = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau".split()
CHAT = ("{% for m in messages %}<|im_start|>{{ m['role'] }}\n{{ m['content'] }}<|im_end|>\n{% endfor %}"
        "{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}")


def fake_rows(n=40, seed=0):
    r = random.Random(seed)
    s = lambda k: " ".join(r.choice(WORDS) for _ in range(k))  # noqa: E731
    return [{"instruction": s(r.randint(3, 8)), "context": s(r.randint(0, 6)) if r.random() < .5 else "",
             "response": s(r.randint(2, 12)), "category": "x"} for _ in range(n)]


def make_tokenizer(out: Path):
    texts = [D.PROMPT_WITH_INPUT, D.PROMPT_NO_INPUT, " ".join(WORDS), "user assistant"]
    pre = pre_tokenizers.Whitespace()
    vocab_words = sorted({w for t in texts for w, _ in pre.pre_tokenize_str(t)})
    specials = ["<|endoftext|>", "<|im_start|>", "<|im_end|>", "[UNK]"]
    vocab = {w: i for i, w in enumerate(specials + vocab_words)}
    tk = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tk.pre_tokenizer = pre
    tk.decoder = decoders.WordPiece(prefix="##")  # joins with spaces
    fast = PreTrainedTokenizerFast(tokenizer_object=tk, unk_token="[UNK]", eos_token="<|endoftext|>",
                                   pad_token="<|endoftext|>", additional_special_tokens=["<|im_start|>", "<|im_end|>"])
    fast.chat_template = CHAT
    fast.save_pretrained(out)
    return fast


def make_model(out: Path, vocab: int, seed: int, layers: int):
    torch.manual_seed(seed)
    cfg = LlamaConfig(vocab_size=vocab, hidden_size=32, intermediate_size=64, num_hidden_layers=layers,
                      num_attention_heads=4, num_key_value_heads=2, max_position_embeddings=512,
                      tie_word_embeddings=True, bos_token_id=None, eos_token_id=2, pad_token_id=0)
    LlamaForCausalLM(cfg).save_pretrained(out)


class FakeEmbedder:  # stands in for sentence-transformers: bag of hashed words
    def __init__(self, *a, **k):
        pass

    def encode(self, texts, **k):
        v = np.zeros((len(texts), 16), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in t.split():
                v[i, hash(w) % 16] += 1
        return v


def setup():
    base = Path(ROOT) / "models"
    tok = make_tokenizer(base / "student")
    tok.save_pretrained(base / "teacher")
    make_model(base / "student", len(tok), 0, 2)
    make_model(base / "teacher", len(tok), 1, 3)
    MODELS.update({"teacher": str(base / "teacher"), "135m": str(base / "student"), "360m": str(base / "student")})
    sys.modules["sentence_transformers"] = types.SimpleNamespace(SentenceTransformer=FakeEmbedder)
    CFG.update({"n_valid": 6, "n_test": 4, "batch_size": 4, "micro_batch": 2, "epochs": 1, "cache_k": 16,
                "rs_samples": 8, "cache_batch": 3, "max_new_tokens": 6, "n_samples": 3, "eval_prompts": 5,
                "gen_batch": 6, "token_eval_examples": 5, "tail_ks": [2, 4, 16], "lr": 1e-3})
    return tok


# ------------------------------------------------------------------ tests
def test_all():
    from cachekd.evaluate import evaluate
    from cachekd.models import load_model, load_tokenizer, response_logits
    from cachekd.targets import Cache, from_logits
    from cachekd.teacher import build_cache, build_seqkd
    from cachekd.train import train
    from cachekd.utils import path, read_jsonl

    setup()
    tok = load_tokenizer("135m")
    info = D.build_splits(tok, fake_rows())
    assert info["test"]["n"] == 4 and info["valid"]["n"] == 6 and 20 <= info["train"]["n"] <= 30, info
    info2 = D.build_splits(tok, fake_rows())
    assert info2 == info, "splits not deterministic"

    # alignment: same response tokens at the same flat positions for student and teacher
    encs = [D.encode(r, tok) for r in D.load_split("train", 3)]
    sb, tb = D.collate(encs, tok.pad_token_id, "student"), D.collate(encs, tok.pad_token_id, "teacher")
    assert torch.equal(sb["targets"], tb["targets"])
    assert all(sb["input_ids"][r, p + 1] == t for r, p, t in zip(sb["rows"], sb["pos"], sb["targets"]))
    assert all(tb["input_ids"][r, p + 1] == t for r, p, t in zip(tb["rows"], tb["pos"], tb["targets"]))
    assert len(encs[0]["t_prompt"]) != len(encs[0]["s_prompt"])  # the two prompt formats really differ

    # response_logits == full forward logits at those positions
    teacher = load_model("teacher", dtype=torch.float32)
    full = teacher(input_ids=tb["input_ids"], attention_mask=tb["attention_mask"]).logits
    assert torch.allclose(response_logits(teacher, tb), full[tb["rows"], tb["pos"]], atol=1e-5)

    # generation survives a CUDA OOM by halving the batch, with identical greedy output
    from cachekd.generate import generate
    ps = [D.encode_ids(D.student_prompt(r), tok) for r in D.load_split("train", 6)]
    ref = generate(teacher, tok, ps, greedy=True, n=1, max_new=5, batch=6)
    real_gen = teacher.generate

    def flaky(*a, **k):
        if k["input_ids"].shape[0] > 2:
            raise RuntimeError("CUDA error: out of memory")
        return real_gen(*a, **k)
    teacher.generate = flaky
    assert generate(teacher, tok, ps, greedy=True, n=1, max_new=5, batch=6) == ref
    teacher.generate = real_gen

    # cache == online targets (up to fp16 storage of probabilities)
    build_cache("train")
    cache = Cache("train")
    rows = D.load_split("train")
    assert cache.meta["positions"] == sum(len(D.encode(r, tok)["resp"]) for r in rows)
    ex = [r["id"] for r in rows[5:8]]
    encs = [D.encode(r, tok) for r in rows[5:8]]
    # compare with the cache's precision (bf16 teacher); a random tiny teacher is near-uniform, so
    # compare probabilities (order among near-ties may flip), not raw id order
    tl = response_logits(load_model("teacher"), D.collate(encs, tok.pad_token_id, "teacher")).float()
    p_on = tl.softmax(-1)
    for k in (4, 16):
        on = from_logits(tl, "tailbucket", k, 0)
        off = cache.targets(ex, "tailbucket", k, torch.device("cpu"))
        assert torch.allclose(p_on.gather(1, off.ids), off.probs, atol=3e-3), k
        assert torch.allclose(on.probs, off.probs, atol=3e-3) and torch.allclose(on.tail, off.tail, atol=3e-3), k
    rs = cache.targets(ex, "rs", None, torch.device("cpu"))
    assert rs.rs_ids.shape == (on.ids.shape[0], CFG["rs_samples"]) and (rs.rs_ids >= 0).all()
    # resumable: a second call is a no-op
    build_cache("train")

    # every arm trains (online and cache), losses finite, results logged once
    build_seqkd()
    sk = read_jsonl(path("teacher", "seqkd_train.jsonl"))
    assert len(sk) == info["train"]["n"]
    # a teacher response cut by max_new_tokens gets no invented end token; a finished one keeps it
    from cachekd.data import end_id
    for r in sk:
        e = D.encode({"instruction": "x", "context": "", "response": ""}, tok, response_ids=r["ids"])
        assert (e["resp"][-1] == end_id(tok)) == r["ended"], r
    # the cache refuses targets built from different token ids
    ex_rows = D.load_split("train", 2)
    bad = [D.encode(r, tok) for r in ex_rows]
    bad[0]["resp"] = bad[0]["resp"][::-1]
    try:
        cache.verify([r["id"] for r in ex_rows], bad)
        raise RuntimeError("verify should have failed")
    except AssertionError:
        pass
    cache.verify([r["id"] for r in ex_rows], [D.encode(r, tok) for r in ex_rows])
    # LR and CE overrides are tagged in the run id and recorded
    r_lr = train("full", "fkl", None, "135m", 4, 5, "online", lr=3e-4, ce_weight=0.5)
    assert r_lr == "full_fkl_lr0.0003_ce0.5_135m_n4_s5_v1", r_lr
    rec = [r for r in read_jsonl(path("runs", "results.jsonl")) if r["run_id"] == r_lr][0]
    assert rec["lr"] == 3e-4 and rec["ce_weight"] == 0.5
    # a save=False run is never "done", so a later real run still trains and saves
    train("sft", None, None, "135m", 4, 99, save=False)
    train("sft", None, None, "135m", 4, 99)
    assert (Path(ROOT) / "ckpt" / "sft_lr0.001_135m_n4_s99_v1" / "config.json").exists()
    # "MiniLLM-style init": KD from an SFT student; tagged in the run id and recorded
    r_init = train("topk", "fkl", 4, "135m", 4, 99, "cache", init_from="sft_lr0.001_135m_n4_s99_v1")
    assert r_init == "topk_k4_fkl_lr0.001_sftinit_135m_n4_s99_v1", r_init
    assert [r for r in read_jsonl(path("runs", "results.jsonl"))
            if r["run_id"] == r_init][0]["init_from"] == "sft_lr0.001_135m_n4_s99_v1"
    arms = [("sft", None, None, "cache"), ("seqkd", None, None, "cache"), ("full", "fkl", None, "online"),
            ("full", "rkl", None, "online"), ("topk", "fkl", 4, "online"), ("topk", "rkl", 4, "cache"),
            ("tailbucket", "fkl", 4, "cache"), ("tailbucket", "rkl", 4, "cache"), ("rs", "fkl", None, "cache"),
            ("rs", "rkl", None, "online"), ("tailuniform", "fkl", 4, "cache")]
    rids = [train(a, d, k, "135m", None, 1, s) for a, d, k, s in arms]
    res = read_jsonl(path("runs", "results.jsonl"))
    arm_rows = [r for r in res if r["run_id"] in rids]
    assert len(arm_rows) == len(arms) and all(np.isfinite(r["final_loss"]) for r in arm_rows)
    train("sft", None, None, "135m", None, 1)  # skipped, already done
    assert len(read_jsonl(path("runs", "results.jsonl"))) == len(res)

    # same seed -> identical training (determinism of order and init)
    r1 = train("topk", "fkl", 4, "135m", 8, 7, "cache")
    r2_loss = [r for r in read_jsonl(path("runs", "results.jsonl")) if r["run_id"] == r1][0]["final_loss"]
    with open(path("runs", "results.jsonl"), "r", encoding="utf-8") as f:
        lines = [l for l in f if r1 not in l]
    with open(path("runs", "results.jsonl"), "w", encoding="utf-8") as f:
        f.writelines(lines)
    train("topk", "fkl", 4, "135m", 8, 7, "cache")
    again = [r for r in read_jsonl(path("runs", "results.jsonl")) if r["run_id"] == r1][0]["final_loss"]
    assert abs(again - r2_loss) < 1e-6, (again, r2_loss)

    # online top-k and cached top-k give (nearly) the same training loss
    a = train("topk", "fkl", 4, "135m", 8, 3, "online")
    b = train("topk", "fkl", 4, "135m", 8, 3, "cache")
    la, lb = [[r for r in read_jsonl(path("runs", "results.jsonl")) if r["run_id"] == x][0]["final_loss"] for x in (a, b)]
    assert abs(la - lb) < 1e-2, (la, lb)

    # evaluation: teacher first (its samples feed the recall metric), then students
    try:  # a student cannot be evaluated before the teacher's samples exist (recall metric)
        evaluate(rids[0], "valid", 5)
        raise RuntimeError("student eval should require teacher samples")
    except AssertionError:
        pass
    evaluate("teacher", "valid", 5)
    for rid in rids[:3]:
        evaluate(rid, "valid", 5)
    ev = read_jsonl(path("runs", "evals.jsonl"))
    assert len(ev) == 4
    st = ev[-1]
    for key in ["tok_goldtail8_frac", "tok_freqb0_n", "tok_freqb4_n"]:
        assert key in st, key
    assert sum(st[f"tok_freqb{i}_n"] for i in range(5)) == st["tok_positions"]
    for key in ["rougeL_greedy", "distinct2", "distinct2_first32", "self_bleu", "semantic_div", "tok_H_q", "tok_ece",
                "tok_q_out_top4", "tok_tail_cov_top4", "tok_tail_cov_mass_top4", "tok_tail_cond_kl_top4",
                "tok_tail_mass_abs_err_top4", "precision_teacher_lp", "recall_student_lp"]:
        assert key in st and (st[key] is None or np.isfinite(st[key]) or key == "self_bleu"), key
    assert 0 <= st["tok_tail_cov_top4"] <= 1 + 1e-6 and 0 <= st["tok_ece"] <= 1
    evaluate(rids[0], "valid", 5)  # skipped
    assert len(read_jsonl(path("runs", "evals.jsonl"))) == 4

    # sampling-temperature sweep on finished checkpoints (resumable, one row + one per-prompt file per point)
    import cachekd.tempsweep as TS
    from cachekd import sweepplan
    pts = sweepplan.points()
    assert len(pts) == 12 and pts[0] == ("topk_k8_fkl_135m_nall_s1_v1", 1.0) \
        and pts[4] == ("full_fkl_135m_nall_s1_v1", 0.7) and pts[-1] == ("tailbucket_k8_fkl_135m_nall_s1_v1", 1.15)
    r_hot = TS.sample_point(rids[2], "valid", 1.3)
    assert r_hot["temperature"] == 1.3 and 0 <= r_hot["semantic_div"] <= 2 and "teacher_lp_seq" in r_hot
    assert TS.sample_point(rids[2], "valid", 1.3) == {}  # skipped, already done
    pp = json.loads(TS.perprompt_file(rids[2], "valid", 1.3).read_text())
    assert len(pp["ids"]) == r_hot["n_prompts"] == 5 and all(len(pp[k]) == 5 for k in TS.PER_PROMPT)
    # the row's means are exactly the means of the per-prompt arrays; token-weighted lp is a weighted mean
    assert abs(np.nanmean(pp["semantic_div"]) - r_hot["semantic_div"]) < 1e-12
    assert abs(np.nanmean(pp["teacher_lp"]) - r_hot["teacher_lp_seq"]) < 1e-9
    assert np.isfinite(r_hot["teacher_lp_tok"]) and r_hot["teacher_lp_tok"] < 0
    assert abs(np.nanmean(pp["teacher_lp_tok"]) - r_hot["teacher_lp_tok"]) < 0.2 and pp["setup"]["temperature"] == 1.3
    assert len(r_hot["perprompt_sha256"]) == 64 and TS.verified(r_hot, TS.perprompt_file(rids[2], "valid", 1.3))
    # a tampered artifact no longer verifies, so resume REDOES the point instead of trusting it
    f_pp = TS.perprompt_file(rids[2], "valid", 1.3)
    f_pp.write_bytes(f_pp.read_bytes().replace(b"dolly_", b"dollx_", 1))
    n_before = len(read_jsonl(path("runs", "tempsweep.jsonl")))
    assert not TS.verified(r_hot, f_pp) and TS.sample_point(rids[2], "valid", 1.3) != {}
    assert len(read_jsonl(path("runs", "tempsweep.jsonl"))) == n_before + 1
    # run_plan runs only unfinished points; the legacy entry point (used by a grid process started before the
    # plan change, once per OLD sweep point) completes the whole current plan once and is then a no-op
    TS.plan_points = lambda: [(rids[2], 0.85), (rids[2], 1.3), ("no_such_run_s1_v1", 1.0)]
    n0 = len(read_jsonl(path("runs", "tempsweep.jsonl")))  # includes the redo row
    sweepplan.SPLIT = "valid"  # the plan, not the legacy call's arguments, decides the split
    assert TS.sample_eval(rids[2], "test", 0.7) == {}  # legacy call with an OLD point: runs the whole plan
    assert TS.sample_eval(rids[2], "test", 1.15) == {}  # ... and the next old points are no-ops
    rows_ts = read_jsonl(path("runs", "tempsweep.jsonl"))
    assert len(rows_ts) == n0 + 1 and {r["temperature"] for r in rows_ts} == {1.3, 0.85}, rows_ts
    failed_ts = [f for f in read_jsonl(path("runs", "failed.jsonl")) if f.get("stage") == "tempsweep"]
    assert failed_ts and failed_ts[-1]["run_id"] == "no_such_run_s1_v1"  # a missing checkpoint is reported, not fatal
    assert TS.sample_eval(rids[2], "valid", 1.3) == {} and len(read_jsonl(path("runs", "tempsweep.jsonl"))) == n0 + 1

    # an external eval set (MiniLLM format, written locally here; the real one is downloaded once)
    from cachekd.utils import write_jsonl
    D.EXTERNAL["vicuna"] = D.EXTERNAL["vicuna"][:3] + (5,)  # expected rows of the local stand-in: no download
    write_jsonl(path("data", "vicuna.jsonl"),
                [{"id": f"vicuna_{i}", "instruction": r["instruction"], "context": "", "response": r["response"],
                  "refs": [r["response"]], "source_category": "x"} for i, r in enumerate(fake_rows(5, seed=9))])
    evaluate("teacher", "vicuna", 5)
    evaluate(rids[0], "vicuna", 5)
    v = [r for r in read_jsonl(path("runs", "evals.jsonl")) if r["split"] == "vicuna"]
    assert len(v) == 2 and "recall_student_lp" in v[1] and "tok_H_q" not in v[1]

    import subprocess
    out = subprocess.run([sys.executable, "cf.py", "table"], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parent.parent, env={**os.environ})
    assert out.returncode == 0 and "teacher" in out.stdout, out.stderr[-2000:]


if __name__ == "__main__":
    test_all()
    print("ALL PIPELINE TESTS PASSED")
