"""Per-position distillation losses for every cache format x {forward KL, reverse KL}.

p = teacher, q = student (full softmax over the vocabulary unless stated), T = teacher top-k set.

  full        FKL  KL(p || q)                     RKL  KL(q || p)
  topk        FKL  KL(p_T/p(T) || q)  (student full softmax: the zero-target tail is pushed to 0;
                   the standard cached top-k loss, Anshumann et al. 2025)
              RKL  KL(q_T/q(T) || p_T/p(T))  (both renormalised on T: "normalised top-k", the usual
                   sparse reverse KL; it gives no gradient to the student's tail mass)
  tailbucket  (k+1)-way: p~ = (p_T, 1 - p(T)), q~ = (q_T, 1 - q(T));  FKL KL(p~||q~), RKL KL(q~||p~)
              (prior work: TAD's top-K term, Dasgupta et al. 2026; TA-OPD, Huang & Wei 2026)
  tailuniform FKL only: KL(p_u || q), p_u = p on T and the exact tail mass 1 - p(T) spread uniformly over the
              V - k other tokens (allocation diagnostic, authors 19 Oct: does the student need the teacher's
              tail SHAPE or only its AMOUNT?)
  rs          p^ = count/N from N teacher draws (Anshumann et al. 2025)
              FKL  KL(p^ || q)    RKL  KL(q_S/q(S) || p^) on the drawn support S (our extension)

All functions take student logits (N, V) and return a loss per position (N,), in nats.
"""
import torch
import torch.nn.functional as F

from .targets import Targets

LOG_FLOOR = 1e-30  # only guards exact zeros inside log(); cached probabilities are fp32


def _plogp(p: torch.Tensor) -> torch.Tensor:
    return torch.special.xlogy(p, p)


def _rkl_terms(lq: torch.Tensor, lp: torch.Tensor) -> torch.Tensor:
    """q (log q - log p) with 0 where q is 0. The log-ratio is masked BEFORE the product so that
    neither the value nor the gradient sees 0 * -inf (torch.where alone leaks nan gradients)."""
    q = lq.exp()
    return q * torch.where(q > 0, lq - lp, torch.zeros_like(q))


def ce(s_logits: torch.Tensor, gold: torch.Tensor) -> torch.Tensor:
    return F.cross_entropy(s_logits.float(), gold, reduction="none")


def full_kl(s_logits, t: Targets, div: str):
    lq = F.log_softmax(s_logits.float(), -1)
    lp = F.log_softmax(t.logits.float(), -1)
    if div == "fkl":
        return (lp.exp() * (lp - lq)).sum(-1)
    return _rkl_terms(lq, lp).sum(-1)


def topk_kl(s_logits, t: Targets, div: str):
    s = s_logits.float()
    phat = t.probs / t.probs.sum(-1, keepdim=True)
    if div == "fkl":
        lq_k = F.log_softmax(s, -1).gather(1, t.ids)
        return _plogp(phat).sum(-1) - (phat * lq_k).sum(-1)
    lqhat = F.log_softmax(s.gather(1, t.ids), -1)
    return _rkl_terms(lqhat, phat.clamp_min(LOG_FLOOR).log()).sum(-1)


def tail_log_mass(s: torch.Tensor, ids: torch.Tensor) -> torch.Tensor:
    """log q(V \\ T), exactly, via logsumexp over the masked logits."""
    return torch.logsumexp(s.scatter(1, ids, float("-inf")), -1) - torch.logsumexp(s, -1)


def tailbucket_kl(s_logits, t: Targets, div: str):
    s = s_logits.float()
    lq = torch.cat([F.log_softmax(s, -1).gather(1, t.ids), tail_log_mass(s, t.ids)[:, None]], -1)
    p = torch.cat([t.probs, t.tail[:, None]], -1)
    p = p / p.sum(-1, keepdim=True)  # absorbs fp16 rounding of the stored probabilities
    if div == "fkl":
        return _plogp(p).sum(-1) - (p * lq).sum(-1)
    return _rkl_terms(lq, p.clamp_min(LOG_FLOOR).log()).sum(-1)


def rs_kl(s_logits, t: Targets, div: str):
    s = s_logits.float()
    if div == "fkl":
        # sum_i p^_i log p^_i - sum_j w_j log q(x_j): the gradient needs only the gathered draws;
        # the entropy constant (reported loss only) is computed without a graph
        with torch.no_grad():
            ent = _plogp(torch.zeros_like(s).scatter_add_(1, t.rs_ids, t.rs_w.float())).sum(-1)
        return ent - (t.rs_w.float() * F.log_softmax(s, -1).gather(1, t.rs_ids)).sum(-1)
    phat = torch.zeros_like(s).scatter_add_(1, t.rs_ids, t.rs_w.float())
    support = phat > 0
    lqhat = F.log_softmax(s.masked_fill(~support, float("-inf")), -1)
    return _rkl_terms(lqhat, phat.clamp_min(LOG_FLOOR).log()).sum(-1)


def tailuniform_kl(s_logits, t: Targets, div: str):
    assert div == "fkl", "the uniform-tail diagnostic is defined for FKL only"
    lq = F.log_softmax(s_logits.float(), -1)
    lq_k = lq.gather(1, t.ids)
    p = t.probs.float()
    tail = t.tail.float().clamp_min(0)
    n_rest = lq.shape[-1] - t.ids.shape[-1]
    u = tail / n_rest  # per-token target mass outside T
    ent = _plogp(p).sum(-1) + n_rest * _plogp(u)
    return ent - (p * lq_k).sum(-1) - u * (lq.sum(-1) - lq_k.sum(-1))


KD = {"full": full_kl, "topk": topk_kl, "tailbucket": tailbucket_kl, "rs": rs_kl, "tailuniform": tailuniform_kl}


def kd_loss(s_logits: torch.Tensor, t: Targets, arm: str, div: str) -> torch.Tensor:
    assert div in ("fkl", "rkl"), div
    return KD[arm](s_logits, t, div)
