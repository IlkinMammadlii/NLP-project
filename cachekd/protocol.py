"""The frozen experimental protocol for the main grid (authors' decisions, 17 Oct 2026).

`cf.py grid` refuses to run until FROZEN_LR is set. Set it ONCE from the dev LR sweep (`cf.py sweep`),
write the protocol into EXPERIMENT_LOG.md with the date, commit, and never edit this file afterwards
(a change means a new CONFIG_VERSION and a logged reason).
"""

FROZEN_LR = 2e-4  # frozen 17 Oct 2026 from `cf.py sweep` (valid ROUGE-L, see EXPERIMENT_LOG.md); never change

STUDENT = "135m"
SEEDS = [1, 2, 3]

# (arm, divergence, k, source). Top-k RKL only at k = 8 and 128; RS with FKL only (Anshumann et al.);
# tail bucket ("ghost token", prior work) at k = 8 with both divergences.
ARMS = [
    ("sft", None, None, "cache"),
    ("seqkd", None, None, "cache"),
    ("topk", "fkl", 8, "cache"),
    ("topk", "fkl", 32, "cache"),
    ("topk", "fkl", 128, "cache"),
    ("topk", "rkl", 8, "cache"),
    ("topk", "rkl", 128, "cache"),
    ("tailbucket", "fkl", 8, "cache"),
    ("tailbucket", "rkl", 8, "cache"),
    ("rs", "fkl", None, "cache"),
    ("full", "fkl", None, "online"),
    ("full", "rkl", None, "online"),
]

# KD + CE ablation (ce_weight = 0.5), FKL only, on top-8 and full KD; 2 seeds if time allows
CE_ABLATION = {"ce_weight": 0.5, "arms": [("topk", "fkl", 8, "cache"), ("full", "fkl", None, "online")],
               "seeds": [1, 2]}

# Run AFTER the main grid (authors, 17 Oct): "MiniLLM-style init" robustness ablation, reported separately.
# KD starts from the same-seed SFT student of the main grid instead of the base checkpoint.
# Extended 18 Oct 2026 (authors): base-init RKL students do not terminate, so H3 (FKL vs RKL) is tested from
# SFT init. The RKL arms run in a second grid pass (appended; no earlier job changes).
# Extended again 18 Oct (authors): the matching FKL arms (top-128, tail bucket) so H3 is a matched 2 x 4
# comparison (FKL/RKL x top-8, top-128, tail bucket, full) from SFT init, seeds {1, 2}.
SFT_INIT_ABLATION = {"arms": [("topk", "fkl", 8, "cache"), ("full", "fkl", None, "online"),
                              ("topk", "fkl", 128, "cache"), ("tailbucket", "fkl", 8, "cache"),
                              ("topk", "rkl", 8, "cache"), ("topk", "rkl", 128, "cache"),
                              ("tailbucket", "rkl", 8, "cache"), ("full", "rkl", None, "online")],
                     "seeds": [1, 2]}

# Sampling-temperature sweep (authors, 19 Oct 2026): runs after the main grid and BEFORE the ablations (the
# "TEMPSWEEP" marker in cf.py grid). The registered plan (arms and temperatures) lives in cachekd/sweepplan.py;
# the analysis plan was committed in HYPOTHESES.md before any point was run. (An earlier version of this entry
# listed T in {0.85, 1.15, 1.3} for top-8 and full KD; it was replaced the same day before it ran.)

# Final numbers: Dolly test first, then Self-Instruct, then Vicuna (S-NI only if time remains).
EVAL_SETS = ["test", "selfinst", "vicuna"]
