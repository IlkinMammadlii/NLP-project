"""Training-loss curves per arm from the run logs, and a convergence check (CPU, reads logs only).

Losses of different objectives are not on one scale, so each curve is shown on its own axis range and the
check is relative. Training loss drops at every epoch boundary (the same examples are seen again), so the
plateau check stays inside the LAST epoch: change of the mean loss from its first to its second half.
Training loss on repeated data is not evidence of generalisation; held-out NLL is reported separately.
  .venv\\Scripts\\python.exe analysis\\loss_curves.py [--seed 1]   -> $CF_ROOT/figures/loss_curves_s<seed>.{pdf,png}
"""
import argparse
import os
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(os.environ.get("CF_ROOT", Path(__file__).resolve().parent.parent / "cf_data"))
ARMS = ["sft", "seqkd", "topk_k8_fkl", "topk_k32_fkl", "topk_k128_fkl", "tailbucket_k8_fkl", "rs_fkl", "full_fkl",
        "topk_k8_rkl", "topk_k128_rkl", "tailbucket_k8_rkl", "full_rkl"]
EPOCHS: dict = {}
STEP = re.compile(r"step (\d+)/(\d+) loss ([0-9.eE+-]+)")


def curve(rid: str):
    """Steps and losses of the LAST training attempt in the run log (a restart logs a new "train <rid>:"
    header and starts again at step 0); None unless that attempt completed (a "done" line follows)."""
    f = ROOT / "logs" / f"{rid}.log"
    if not f.exists():
        return None
    lines = f.read_text(encoding="utf-8", errors="replace").split("\n")
    starts = [i for i, ln in enumerate(lines) if f"] train {rid}:" in ln]
    seg = lines[starts[-1]:] if starts else lines
    if not any(f"] done {rid}" in ln for ln in seg):
        return None
    pts = {}
    for line in seg:
        m = STEP.search(line)
        if m:
            pts[int(m.group(1))] = (int(m.group(2)), float(m.group(3)))
    if not pts:
        return None
    steps = np.array(sorted(pts))
    total = pts[steps[-1]][0]
    return steps, np.array([pts[s][1] for s in steps]), total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    global EPOCHS
    import json
    EPOCHS = {}
    for line in (ROOT / "runs" / "results.jsonl").read_text(encoding="utf-8").split("\n"):
        if line.strip():
            r = json.loads(line)
            EPOCHS[r["run_id"]] = r["cfg"]["epochs"]
    fig, axes = plt.subplots(3, 4, figsize=(12, 7.5))
    print(f"{'arm':20s} {'final loss':>10s} {'last epoch: 2nd half vs 1st half':>34s}")
    for ax, arm in zip(axes.flat, ARMS):
        c = curve(f"{arm}_135m_nall_s{a.seed}_v1")
        ax.set_title(arm, fontsize=9)
        if c is None:
            ax.text(0.5, 0.5, "no log", ha="center", transform=ax.transAxes)
            continue
        steps, loss, total = c
        ax.plot(steps, loss, lw=1)
        ax.set_xlabel("step", fontsize=8)
        ax.tick_params(labelsize=7)
        n_ep = EPOCHS.get(f"{arm}_135m_nall_s{a.seed}_v1", 3)
        e3 = (n_ep - 1) * total / n_ep + 20  # last epoch, skipping the steps right at its boundary
        mid_e3 = (e3 + total) / 2
        first = loss[(steps >= e3) & (steps < mid_e3)].mean()
        second = loss[steps >= mid_e3].mean()
        rel = (second - first) / abs(first)
        for b in (total * j / n_ep for j in range(1, n_ep)):
            ax.axvline(b, color="0.7", lw=0.6, ls="--")
        print(f"{arm:20s} {loss[-1]:10.4f} {100 * rel:33.1f}%")
    fig.suptitle(f"Training loss per arm (seed {a.seed}); each panel on its own scale", fontsize=10)
    fig.tight_layout()
    out = ROOT / "figures"
    out.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(out / f"loss_curves_s{a.seed}.{ext}", dpi=130)
    print(f"figure -> {out / f'loss_curves_s{a.seed}.png'}")


if __name__ == "__main__":
    main()
