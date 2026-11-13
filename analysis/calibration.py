"""Next-token calibration per student: ECE, accuracy, NLL, Brier and reliability bins (GPU, student only;
run AFTER the grid). Same 300 held-out Dolly-test gold responses and positions as the main evaluation
(teacher-forced); confidence = max_i q_i, correct = argmax q == gold next token, 15 equal-width bins.
Brier = sum_i (q_i - 1[i = gold])^2 (multi-class). Rows -> runs/calibration.jsonl (resumable);
figure -> figures/reliability_test.{pdf,png} for the seed-1 forward-KL arms.

  .venv\\Scripts\\python.exe analysis\\calibration.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cachekd import data as D  # noqa: E402
from cachekd import metrics as M  # noqa: E402
from cachekd.config import CFG  # noqa: E402
from cachekd.evaluate import model_ref  # noqa: E402
from cachekd.models import load_model, load_tokenizer, response_logits  # noqa: E402
from cachekd.utils import append_jsonl, path, read_jsonl, root  # noqa: E402

FIG_ARMS = ["topk_k8_fkl", "topk_k32_fkl", "topk_k128_fkl", "tailbucket_k8_fkl", "rs_fkl", "full_fkl"]


@torch.no_grad()
def calib(run: str, tok, rows: list) -> dict:
    name, _ = model_ref(run)
    model = load_model(name)
    conf, correct, nll, brier = [], [], [], []
    encs = [D.encode(r, tok) for r in rows]
    for i in range(0, len(encs), 4):
        b = D.collate(encs[i:i + 4], tok.pad_token_id, "student")
        lq = F.log_softmax(response_logits(model, b), -1)
        q = lq.exp()
        gold = b["targets"].to(q.device)
        c, pred = q.max(-1)
        q_gold = q.gather(1, gold[:, None])[:, 0]
        conf.append(c.cpu()); correct.append((pred == gold).float().cpu())
        nll.append(-lq.gather(1, gold[:, None])[:, 0].cpu())
        brier.append((1 - 2 * q_gold + (q * q).sum(-1)).cpu())
    conf, correct = torch.cat(conf).numpy().astype(float), torch.cat(correct).numpy()
    edges = np.arange(1, CFG["ece_bins"] + 1) / CFG["ece_bins"]
    b = np.searchsorted(edges, conf, side="left")
    bins = [{"n": int((b == k).sum()), "conf": float(conf[b == k].mean()) if (b == k).any() else None,
             "acc": float(correct[b == k].mean()) if (b == k).any() else None} for k in range(CFG["ece_bins"])]
    del model
    torch.cuda.empty_cache()
    from cachekd.config import CONFIG_VERSION
    return {"run_id": run, "split": "test", "n_examples": len(rows), "config_version": CONFIG_VERSION,
            "positions": int(len(conf)),
            "ece": M.expected_calibration_error(conf, correct, CFG["ece_bins"]), "accuracy": float(correct.mean()),
            "nll": float(torch.cat(nll).mean()), "brier": float(torch.cat(brier).mean()), "bins": bins}


def main():
    out_f = path("runs", "calibration.jsonl")
    from cachekd.config import CONFIG_VERSION
    done = {r["run_id"] for r in read_jsonl(out_f)  # resume only rows made on the same positions and config
            if r.get("split") == "test" and r.get("n_examples") == CFG["token_eval_examples"]
            and r.get("config_version") == CONFIG_VERSION}
    tok = load_tokenizer("135m")
    rows = D.load_split("test", CFG["token_eval_examples"])
    runs = ["135m"] + sorted({r["run_id"] for r in read_jsonl(path("runs", "results.jsonl"))
                              if r.get("n_train", 0) > 5000 and (root() / "ckpt" / r["run_id"] / "config.json").exists()})
    for run in runs:
        if run in done:
            continue
        rec = calib(run, tok, rows)
        append_jsonl(out_f, rec)
        print(f"{run}: ECE {rec['ece']:.4f} acc {rec['accuracy']:.4f} NLL {rec['nll']:.4f} Brier {rec['brier']:.4f}",
              flush=True)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    recs = {r["run_id"]: r for r in read_jsonl(out_f) if r.get("n_examples") == CFG["token_eval_examples"]}
    fig, ax = plt.subplots(figsize=(4.2, 4))
    ax.plot([0, 1], [0, 1], color="0.6", ls="--", lw=0.8)
    for arm in FIG_ARMS:
        r = recs.get(f"{arm}_135m_nall_s1_v1")
        if r:
            pts = [(x["conf"], x["acc"]) for x in r["bins"] if x["n"] >= 30]
            ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", ms=3, lw=1,
                    label=f"{arm.replace('_fkl', '')} (ECE {r['ece']:.3f})")
    ax.set_xlabel("confidence (max next-token probability)")
    ax.set_ylabel("accuracy (argmax = gold token)")
    ax.legend(fontsize=6)
    ax.set_title("Reliability, Dolly test, seed 1, forward KL", fontsize=8)
    fig.tight_layout()
    for ext in ("pdf", "png"):
        fig.savefig(path("figures", f"reliability_test.{ext}"), dpi=130)


if __name__ == "__main__":
    main()
