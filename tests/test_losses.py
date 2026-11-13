"""Math checks for the KD losses and teacher targets (CPU, no downloads). Run: python tests/test_losses.py"""
import math
import sys
from pathlib import Path

import torch
import torch.nn.functional as F

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cachekd.losses import kd_loss, tail_log_mass  # noqa: E402
from cachekd.targets import Targets, from_logits, rs_draws, slice_topk, topk_tail  # noqa: E402

torch.manual_seed(0)
N, V = 6, 50


def rand_logits(scale=3.0):
    return torch.randn(N, V, dtype=torch.float64) * scale


def kl(p, q):
    return (p * (p.log() - q.log())).sum(-1)


def test_full_matches_definition_and_zero_at_match():
    s, t = rand_logits(), rand_logits()
    p, q = t.softmax(-1), s.softmax(-1)
    T = Targets(logits=t)
    assert torch.allclose(kd_loss(s, T, "full", "fkl").double(), kl(p, q), atol=1e-5)
    assert torch.allclose(kd_loss(s, T, "full", "rkl").double(), kl(q, p), atol=1e-5)
    assert kd_loss(t, T, "full", "fkl").abs().max() < 1e-5
    assert kd_loss(t, T, "full", "rkl").abs().max() < 1e-5


def test_topk_tail_exact():
    t = rand_logits()
    p = t.softmax(-1)
    ids, probs, tail = topk_tail(t, 7)
    assert torch.allclose(probs.double(), p.gather(1, ids), atol=1e-6)
    assert torch.allclose(tail.double(), 1 - p.gather(1, ids).sum(-1), atol=1e-6)
    assert (probs[:, :-1] >= probs[:, 1:]).all()
    # slicing a larger cache gives the same targets as computing a smaller k directly
    ids32, probs32, tail32 = topk_tail(t, 32)
    sl = slice_topk(ids32, probs32, tail32, 7)
    assert torch.equal(sl.ids, ids) and torch.allclose(sl.probs, probs) and torch.allclose(sl.tail, tail, atol=1e-6)


def test_topk_fkl_is_kl_to_renormalised_target_with_full_student():
    s, t = rand_logits(), rand_logits()
    q = s.softmax(-1)
    T = from_logits(t, "topk", 5, 0)
    phat = T.probs.double() / T.probs.double().sum(-1, keepdim=True)
    want = (phat * (phat.log() - q.gather(1, T.ids).log())).sum(-1)
    assert torch.allclose(kd_loss(s, T, "topk", "fkl").double(), want, atol=1e-5)
    # k = V: renormalised top-k is the full distribution
    assert torch.allclose(kd_loss(s, from_logits(t, "topk", V, 0), "topk", "fkl"),
                          kd_loss(s, Targets(logits=t), "full", "fkl"), atol=1e-5)


def test_topk_rkl_both_renormalised_and_blind_to_tail():
    s, t = rand_logits(), rand_logits()
    T = from_logits(t, "topk", 5, 0)
    phat = T.probs.double() / T.probs.double().sum(-1, keepdim=True)
    qhat = s.gather(1, T.ids).softmax(-1)
    assert torch.allclose(kd_loss(s, T, "topk", "rkl").double(), kl(qhat, phat), atol=1e-5)
    # moving the student's tail logits does not change the loss (no tail gradient)
    s2 = s.clone()
    mask = torch.ones_like(s, dtype=torch.bool).scatter(1, T.ids, False)
    s2[mask] += 5.0
    assert torch.allclose(kd_loss(s2, T, "topk", "rkl"), kd_loss(s, T, "topk", "rkl"), atol=1e-6)


def test_tailbucket_chain_rule_decomposition():
    """KL(p||q) = KL(p~||q~) + p(tail) * KL(p_tail/p(tail) || q_tail/q(tail))."""
    s, t = rand_logits(), rand_logits()
    p, q = t.softmax(-1), s.softmax(-1)
    T = from_logits(t, "tailbucket", 6, 0)
    mask = torch.ones_like(p, dtype=torch.bool).scatter(1, T.ids, False)
    pt, qt = p * mask, q * mask
    pt_n, qt_n = pt / pt.sum(-1, keepdim=True), qt / qt.sum(-1, keepdim=True)
    inner = torch.where(mask, pt_n * (pt_n.clamp_min(1e-300).log() - qt_n.clamp_min(1e-300).log()), 0).sum(-1)
    lhs = kl(p, q)
    rhs = kd_loss(s, T, "tailbucket", "fkl").double() + pt.sum(-1) * inner
    assert torch.allclose(lhs, rhs, atol=1e-5)
    # (k+1)-way RKL by definition
    pb = torch.cat([p.gather(1, T.ids), pt.sum(-1, keepdim=True)], -1)
    qb = torch.cat([q.gather(1, T.ids), qt.sum(-1, keepdim=True)], -1)
    assert torch.allclose(kd_loss(s, T, "tailbucket", "rkl").double(), kl(qb, pb), atol=1e-5)
    assert torch.allclose(tail_log_mass(s, T.ids).exp(), qt.sum(-1), atol=1e-8)


def test_tailbucket_gives_tail_gradient_topk_rkl_does_not():
    s, t = rand_logits(), rand_logits()
    for arm, div, expect in [("tailbucket", "fkl", True), ("tailbucket", "rkl", True), ("topk", "rkl", False)]:
        T = from_logits(t, arm, 5, 0)
        x = s.clone().requires_grad_(True)
        kd_loss(x, T, arm, div).sum().backward()
        mask = torch.ones_like(s, dtype=torch.bool).scatter(1, T.ids, False)
        g = x.grad[mask].abs().max().item()
        assert (g > 1e-6) == expect, (arm, div, g)


def test_rs_unbiased_and_rkl_restricted():
    s, t = rand_logits(), rand_logits(1.0)
    q = s.softmax(-1)
    gen = torch.Generator().manual_seed(0)
    ids, w = rs_draws(t, 200_000, gen)
    T = Targets(rs_ids=ids, rs_w=w)
    # cross-entropy part converges to the full forward KL's cross-entropy: E[-sum p^ log q] = -sum p log q
    phat = torch.zeros_like(s).scatter_add_(1, ids, w.double())
    ce_rs = -(phat * q.log()).sum(-1)
    ce_full = -(t.softmax(-1) * q.log()).sum(-1)
    assert torch.allclose(ce_rs, ce_full, rtol=2e-2), (ce_rs, ce_full)
    assert torch.isfinite(kd_loss(s, T, "rs", "fkl")).all()
    # small-sample RKL: finite, non-negative, gradient finite and zero off the drawn support
    ids, w = rs_draws(t, 8, gen)
    T = Targets(rs_ids=ids, rs_w=w)
    x = s.clone().requires_grad_(True)
    loss = kd_loss(x, T, "rs", "rkl")
    assert torch.isfinite(loss).all() and (loss > -1e-6).all()
    loss.sum().backward()
    assert torch.isfinite(x.grad).all()
    support = torch.zeros_like(s, dtype=torch.bool).scatter(1, ids, True)
    assert x.grad[~support].abs().max() == 0


def test_gradients_finite_with_fp16_underflow():
    s, t = rand_logits(), rand_logits(20.0)  # very peaked teacher: many top-k probs underflow in fp16
    T = from_logits(t, "tailbucket", 16, 0)
    T = Targets(ids=T.ids, probs=T.probs.half().float(), tail=T.tail.float())
    for arm in ("topk", "tailbucket"):
        for div in ("fkl", "rkl"):
            x = s.float().clone().requires_grad_(True)
            loss = kd_loss(x, T, arm, div)
            loss.sum().backward()
            assert torch.isfinite(loss).all() and torch.isfinite(x.grad).all(), (arm, div)


def test_k1_topk_equals_argmax_ce():
    s, t = rand_logits(), rand_logits()
    T = from_logits(t, "topk", 1, 0)
    ce = F.cross_entropy(s, t.argmax(-1), reduction="none")
    assert torch.allclose(kd_loss(s, T, "topk", "fkl").double(), ce, atol=1e-5)


def test_tailuniform_is_kl_to_explicit_uniform_tail_target():
    s, t = rand_logits(), rand_logits()
    T = from_logits(t, "tailuniform", 8, 0)
    p = torch.full_like(t, 0.0)
    V = t.shape[-1]
    p += (T.tail.double() / (V - 8))[:, None]
    p.scatter_(1, T.ids, T.probs.double())
    assert torch.allclose(p.sum(-1), torch.ones(p.shape[0], dtype=p.dtype), atol=1e-6)
    ref = kl(p, F.softmax(s, -1))
    assert torch.allclose(kd_loss(s, T, "tailuniform", "fkl").double(), ref, atol=1e-5)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("ALL LOSS TESTS PASSED")
