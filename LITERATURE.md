# Literature and novelty check

Fresh search on **17 Oct 2026**, as the brief requires ("verify before you build"). Redo it right before
submission: this area moves monthly. Sources below were read from their arXiv/ACL pages. "Read" means
abstract + method section; full papers still need a careful read where marked.

## Verdict

| Claim in the brief | Status after the search | Consequence |
|---|---|---|
| **H4: the tail bucket ((k+1)-way target with the teacher's leftover mass) is our method** | **Not new, three times over.** It is the **"ghost token" of Anshumann et al. (ACL 2025, §3.2 / App. A.5)**, the very paper the brief names as closest: one extra class with the accumulated non-top-K mass for teacher and student; it improved LM loss and calibration over plain top-K but stayed worse than full KD, which motivated their RS-KD. The exact (k+1)-way loss with a "tail token" is the main method of **TA-OPD** (Huang & Wei, arXiv 2608.14728, Aug 2026), for *on-policy reverse KL* on math reasoning. **TAD** (Dasgupta, Cohn & Baldwin, arXiv 2602.20816, 2026) uses the same (K+1)-way term inside a decoupled *forward*-KL loss (plus a re-weighted tail KL) for pre-training/SFT distillation. | Brief's fallback applies: **cite all three (Anshumann's ghost token first), keep the tail bucket as a baseline, do not claim it as ours.** What is still new: evaluating it for diversity, tail knowledge and calibration in the cached off-policy setting, under both FKL and RKL. |
| **Gap: nobody connects cache format with diversity / long-tail knowledge** | **Still open, but the mechanism half is taken.** *The Gradient That Top-k Distillation Never Computes* (arXiv 2610.15589, 11 Oct 2026) proves renormalised top-k KD gives no gradient to tail mass and shows the student's tail mass drifts away from the teacher's. It measures held-out KL, perplexity, tail mass and one ECE point, **not** output diversity, semantic diversity, or long-tail factual knowledge, and it studies forward KL only (RKL arm scoped, not run). Anshumann et al. (ACL 2025) measure accuracy and calibration, not diversity. TA-OPD tracks tail probability and entropy as training diagnostics only. | Our contribution must be framed as the **behavioural consequences** (diversity of generations, coverage, rare-fact recall, calibration) and the **FKL vs RKL interaction**, building on 2610.15589's mechanism, which we must cite prominently. "Tail mass drifts" is not a finding we can claim as new. |
| H1–H3 (sharpening, tail knowledge, FKL/RKL interaction) | No paper found that tests top-k caching against PopQA-by-popularity or semantic diversity. A long-tail search found PopQA (Mallen et al., 2023), Kandpal et al. (ICML 2023) and a 2026 long-tail survey, but no distillation study. | Novel as measurements. Keep them as the core of the paper. |
| ACL 2027 theme | Confirmed on 2027.aclweb.org/calls/main: theme "Homogenization and knowledge collapse in LLMs"; ARR deadline **4 Jan 2027** (AoE); long 8 pages, short 4 (+1 final); conference 20–22 Aug 2027 (tutorials 17, workshops 18–19). | The brief's "17–22 Aug" spans tutorials to the main days; fine. |

**Diversity check (17 Oct, authors' request).** None of the three tail-bucket papers measures output
diversity: TAD reports few-shot accuracy and Full-ECE (the word "divers" does not occur); TA-OPD reports
Avg@8 / Pass@1 accuracy and tracks token entropy and tail probability during training only as collapse
diagnostics; Anshumann et al. report LM loss, downstream accuracy and ECE. So H4 stays a question we can
answer: does the ghost-token correction also recover diversity and tail coverage?

## Papers to cite (and how we relate)

**Sparse / cached-logit KD (closest).**
- Anshumann et al., *Sparse Logit Sampling: Accelerating Knowledge Distillation in LLMs*, ACL 2025 (2025.acl-long.885). Top-k caching is biased; proposes Random Sampling KD. Details we implement: proposal = teacher at τ = 1, **N = 50 draws** (appendix; ≈ 12 unique tokens), target = count/N, forward KL against the student's full softmax; FKL beat RKL in their loss ablation. Their code repo (akhilkedia/RandomSamplingKD) had no code yet when checked. → our `rs` arm. **Their Top-K baseline is unnormalised** (Σ_{i∈K} t_i log(t_i/p_i), student full softmax). It has the same optimum as our renormalised version (the brief's "renormalised top-k"), but its gradient at each position is scaled by the top-k mass (1 − tail), so it down-weights high-entropy positions. Say this in the paper; optionally add their variant as one extra row at k = 8.
- *The Gradient That Top-k Distillation Never Computes*, arXiv 2610.15589 (Oct 2026; authors not on the HTML page, code at KurbanIntelligenceLab/top-k-distillation). Mechanism paper (above). Also proposes caching the teacher's final hidden state to rebuild full logits. **Must read in full.** Also a possible extra arm: hidden-state cache (exact full KD at higher storage).
- Dasgupta, Cohn & Baldwin, *Don't Ignore the Tail: Decoupling top-K Probabilities for Efficient LM Distillation* (TAD), arXiv 2602.20816 (v3 May 2026). (K+1)-way term + re-weighted tail term. Reports RKL gave the worst calibration among their baselines.
- Huang & Wei, *Tail-Aware Top-k On-Policy Distillation* (TA-OPD), arXiv 2608.14728 (Aug 2026). Exact tail-bucket loss, on-policy RKL, math. Notes normalised top-k lets student tail probability and entropy *grow* (on-policy). Relevant to H1's direction: top-k may make students *less* sharp in some settings. Our measurement can arbitrate.
- *Efficient Knowledge Distillation for LLMs: Offline Top-K Logits and a Fused Chunked KL Loss*, arXiv 2608.03796 (Aug 2026). Top-100 cache matches online KD in training loss; no diversity. Useful as "practitioners believe top-k is safe" motivation.
- BiLD (arXiv 2406.13555): clipping to top-k logits *helped* in their setting. A counterpoint to cite.

**Divergence choice and diversity.**
- MiniLLM (Gu et al., ICLR 2024; arXiv 2306.08543): reverse KL; our benchmark protocol. Their data script drops prompts > 256 tokens and keeps 1,000 dev examples; they score ROUGE-L by *sampling* at temperature 1 averaged over 5 seeds (we report greedy, as the brief says, and also sampled ROUGE-L).
- DistiLLM (Ko et al., ICML 2024): skew KL; same benchmark.
- Luong, Tran & Chen, *Diversity-Aware Reverse KL for LLM Distillation* (DRKL), arXiv 2604.00223 (Mar 2026, under review): RKL lowers diversity, weak non-target supervision hurts tail alignment.
- Jin et al., *Entropy-Aware On-Policy Distillation* (EOPD), ICML 2026 (PMLR v306; arXiv 2603.07079): adds FKL at high-entropy teacher tokens because RKL reduces diversity.
- *A Survey of On-Policy Distillation for LLMs*, arXiv 2604.00626.
- *One Symptom, Three Levers: A Critical Review of On-Policy Self-Distillation*, arXiv 2608.25936 (diversity collapse; FKL at high-entropy tokens). The brief's "On-Policy Self-Distillation Reduces Output Diversity" link should be checked against this; it may be the same or a different paper.

**Homogenization and long-tail knowledge.**
- *Quantification of Large Language Model Distillation*, ACL 2025 (2025.acl-long.248): distillation homogenizes models.
- Mallen et al., *When Not to Trust Language Models* (PopQA), ACL 2023. Kandpal et al., *LLMs Struggle to Learn Long-Tail Knowledge*, ICML 2023. *Long-Tail Knowledge in LLMs* survey, arXiv 2602.16201.

## Still to read in full before writing
1. arXiv 2610.15589 (whole paper, especially the drift experiment and SmolLM2 runs: they used SmolLM2-1.7B / 360M, i.e. our family).
2. Anshumann et al. Appendix (which top-k loss variant; their N).
3. TAD and TA-OPD method sections (to describe our tail-bucket baseline precisely and credit them).
4. DRKL and EOPD (to position the FKL/RKL axis).
5. Check the brief's links for "divergence tokens (ICLR 2026)" and "channel-location work" (subliminal learning). We avoid that area, so this only needs a citation check.

## Positioning sentence (draft)
*Prior work shows that renormalised top-k caching biases the teacher signal (Anshumann et al., 2025) and leaves the student's tail mass ungoverned (2610.15589); tail-aware losses (TAD; TA-OPD) restore it for accuracy. We ask what this does to what students **generate**: semantic and lexical diversity, coverage of the teacher's distribution, recall of rare facts, and calibration, and whether the answer flips between forward and reverse KL.*
