"""The registered temperature-sweep plan (authors, 19 Oct 2026; analysis plan in HYPOTHESES.md).

Core points first, then the extras ("if time allows"). T = 1.0 is regenerated inside the sweep so that every
point goes through the same code path (and T = 1.0 can be checked against the main evaluation).
"""
from .config import run_id

SEED = 1          # training seed of the checkpoints
SPLIT = "test"    # Dolly test, 500 prompts
STUDENT = "135m"

# (arm, divergence, k, temperatures), in execution order
PLAN = [
    ("topk", "fkl", 8, (1.0, 1.15, 1.3, 1.5)),
    ("full", "fkl", None, (0.7, 0.85, 1.0, 1.15)),
    # extras
    ("rs", "fkl", None, (1.0, 1.15)),
    ("tailbucket", "fkl", 8, (1.0, 1.15)),
]
CORE_ARMS = 2


def points(plan=None, seed: int | None = None) -> list:
    """[(run_id, temperature), ...] in execution order (frozen LR 2e-4 -> untagged run ids)."""
    out = []
    for arm, div, k, temps in (PLAN if plan is None else plan):
        rid = run_id(arm, div, k, STUDENT, None, SEED if seed is None else seed, "cache")
        out += [(rid, float(t)) for t in temps]
    return out
