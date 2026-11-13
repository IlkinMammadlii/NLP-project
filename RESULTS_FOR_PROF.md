# What the Cache Forgets: key results
SmolLM2-1.7B-Instruct → SmolLM2-135M, Dolly, 3 seeds, Dolly test set (500 prompts). Mean ± std over seeds.

## Table 1: Main grid (forward KL unless noted)
| Teacher signal | ROUGE-L (greedy) ↑ | Semantic diversity ↑ | Self-BLEU ↓ | ECE ↓ | Rare-token loss ↓ |
|---|---|---|---|---|---|
| SFT (gold tokens) | 27.5 ± 0.2 | 0.393 | 19.0 | 0.110 | 5.39 |
| Seq-KD | 19.2 ± 0.2 | 0.248 | 31.3 | 0.257 | 6.31 |
| **Top-8 cache** | 18.6 ± 0.1 | **0.317** | 13.6 | **0.107** | **5.32** |
| Top-32 cache | 18.5 ± 0.2 | 0.339 | 10.9 | 0.076 | 5.02 |
| Top-128 cache | 18.5 ± 0.3 | 0.351 | 10.0 | 0.065 | 4.87 |
| Top-8 + tail bucket | 18.7 ± 0.3 | 0.350 | 10.3 | 0.069 | 5.01 |
| RS-KD (random sampling) | 18.6 ± 0.2 | 0.367 | 9.4 | 0.060 | 4.78 |
| **Full KD** (all logits) | 18.7 ± 0.2 | **0.369** | 9.0 | **0.057** | **4.75** |

**Takeaway:** same greedy quality (≈18.6), but top-8 is 14% less diverse, has 1.9× the calibration error and is worse on rare tokens. All of these change monotonically with k.

## Table 2: Pre-registered matched-temperature test (top-8 − full KD, seeds 1–2, paired bootstrap)
| Comparison | Δ reference overlap [95% CI] | Δ semantic diversity [95% CI] | Verdict |
|---|---|---|---|
| A: top-8 @ T=1.15 vs full @ T=1.0 | −0.49 [−0.70, −0.30] (equivalent) | +0.016 [+0.011, +0.021] | inconclusive (above margin) |
| B: top-8 @ T=1.0 vs full @ T=0.85 | −0.86 [−1.10, −0.66] (equivalent) | +0.023 [+0.019, +0.028] | inconclusive (above margin) |

**Takeaway:** once both students are equally good, top-8 is *not* less diverse; it is just sharper. That means it sits at a different point on the quality–diversity frontier, not below it.

## Table 3: Tail amount vs. tail identity (seed 1)
| Arm | Tail mass | Within-tail KL ↓ | Rare-token loss ↓ | Semantic diversity | ECE ↓ |
|---|---|---|---|---|---|
| Top-8 | 0.177 | 2.14 | 5.31 | 0.316 | 0.106 |
| Top-8 + uniform tail | 0.279 | **2.13** | 5.08 | 0.491 | 0.046 |
| Full KD | 0.236 | 1.71 | 4.73 | 0.370 | 0.055 |

**Takeaway:** giving back only the *amount* of tail mass fixes diversity and calibration. It does not fix *which* tail tokens get that mass.

## Robustness
- **With gold-token CE added (weight 0.5):** ROUGE-L rises to about 23 for both students. The gaps remain: diversity 0.356 (top-8) vs 0.400 (full KD), ECE 0.074 vs 0.040.
- **Out-of-distribution:** the same ordering holds on Self-Instruct (0.492 vs 0.556) and on Vicuna (0.346 vs 0.406).
