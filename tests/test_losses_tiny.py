"""Every loss on a 4-token vocabulary, checked against hand-written formulas in plain Python (no torch in
the reference), one position. These are the equations stated in the paper's Method section.

p = teacher (0.5, 0.3, 0.15, 0.05), q = student softmax of logits (1, 0, 0.5, -1), top-k set T = {0, 1}.
Run: python tests/test_losses_tiny.py
"""
import math
import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cachekd.losses import ce, kd_loss  # noqa: E402
from cachekd.targets import Targets  # noqa: E402

P = [0.5, 0.3, 0.15, 0.05]
Z = [1.0, 0.0, 0.5, -1.0]
Q = [math.exp(z) / sum(math.exp(x) for x in Z) for z in Z]
T = [0, 1]
OUT = [2, 3]


def kl(a, b):
    return sum(x * math.log(x / y) for x, y in zip(a, b) if x > 0)


def logits_tensor():
    return torch.tensor([Z], dtype=torch.float64)


def teacher_targets(kind):
    p = torch.tensor([P], dtype=torch.float64)
    if kind == "full":
        return Targets(logits=p.log())
    ids = torch.tensor([T])
    return Targets(ids=ids, probs=p[:, T], tail=torch.tensor([sum(P[i] for i in OUT)], dtype=torch.float64))


def check(name, got, want):
    assert abs(got - want) < 2e-6, f"{name}: got {got}, want {want}"  # losses run in fp32 by design
    print(f"ok {name:28s} {want:.6f}")


def test_full():
    # FKL = sum_i p_i log(p_i/q_i);  RKL = sum_i q_i log(q_i/p_i)
    check("full FKL", kd_loss(logits_tensor(), teacher_targets("full"), "full", "fkl").item(), kl(P, Q))
    check("full RKL", kd_loss(logits_tensor(), teacher_targets("full"), "full", "rkl").item(), kl(Q, P))


def test_topk():
    # FKL: target p_T / p(T) on T, student FULL softmax q:  sum_{i in T} phat_i log(phat_i / q_i)
    pT = sum(P[i] for i in T)
    phat = [P[i] / pT for i in T]
    check("top-2 FKL", kd_loss(logits_tensor(), teacher_targets("topk"), "topk", "fkl").item(),
          sum(ph * math.log(ph / Q[i]) for ph, i in zip(phat, T)))
    # RKL: both renormalised on T:  KL( q_T/q(T) || p_T/p(T) )
    qT = sum(Q[i] for i in T)
    qhat = [Q[i] / qT for i in T]
    check("top-2 RKL", kd_loss(logits_tensor(), teacher_targets("topk"), "topk", "rkl").item(), kl(qhat, phat))


def test_tailbucket():
    # (k+1)-way: p~ = (p_T, 1 - p(T)), q~ = (q_T, 1 - q(T))
    pt = [P[i] for i in T] + [sum(P[i] for i in OUT)]
    qt = [Q[i] for i in T] + [sum(Q[i] for i in OUT)]
    check("tail bucket FKL", kd_loss(logits_tensor(), teacher_targets("tb"), "tailbucket", "fkl").item(), kl(pt, qt))
    check("tail bucket RKL", kd_loss(logits_tensor(), teacher_targets("tb"), "tailbucket", "rkl").item(), kl(qt, pt))


def test_rs():
    # draws x = (0, 0, 1, 2) with weight 1/4 each -> p^ = (0.5, 0.25, 0.25, 0)
    draws = [0, 0, 1, 2]
    phat = [draws.count(i) / len(draws) for i in range(4)]
    t = Targets(rs_ids=torch.tensor([draws]), rs_w=torch.full((1, 4), 0.25, dtype=torch.float64))
    # FKL: KL(p^ || q) with the student's full softmax
    check("RS FKL", kd_loss(logits_tensor(), t, "rs", "fkl").item(), kl(phat, Q))
    # RKL (our extension, not in the grid): student renormalised on the drawn support S = {0, 1, 2}
    S = [i for i in range(4) if phat[i] > 0]
    qS = sum(Q[i] for i in S)
    check("RS RKL (support-restricted)", kd_loss(logits_tensor(), t, "rs", "rkl").item(),
          kl([Q[i] / qS for i in S], [phat[i] for i in S]))


def test_ce():
    # SFT and seq-KD: -log q(gold)
    check("CE (SFT / seq-KD), gold=2", ce(logits_tensor(), torch.tensor([2])).item(), -math.log(Q[2]))


def test_tailbucket_decomposition():
    # KL(p || q) = KL(p~ || q~) + p(out) KL(p_out/p(out) || q_out/q(out))  -> the tail bucket drops exactly
    # the second term: the mass inside the tail is matched, its distribution over tail tokens is not
    po, qo = sum(P[i] for i in OUT), sum(Q[i] for i in OUT)
    inner = kl([P[i] / po for i in OUT], [Q[i] / qo for i in OUT])
    tb = kd_loss(logits_tensor(), teacher_targets("tb"), "tailbucket", "fkl").item()
    check("chain rule: full = tb + p(out)*inner", tb + po * inner, kl(P, Q))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL TINY LOSS TESTS PASSED")
