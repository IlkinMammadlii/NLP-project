# Hypotheses (pre-registered before the main grid)

Committed to git **before any grid run**. The commit timestamp is the pre-registration. Never edit
this file afterwards; append a dated "Changes" note instead. Decisions use the validation split only; the
test split is for final numbers.

Question: how does the format of cached teacher logits (hard tokens, top-k, sampled, full) change a
distilled student's output diversity, long-tail coverage and calibration, and does that interact with
the divergence (forward vs reverse KL)?

- **H1 (sharpening).** Students trained on renormalised top-k targets with forward KL have lower
  semantic diversity and distinct-2, lower student entropy relative to the teacher, and less mass outside
  the teacher's top-k than full-KD students, while greedy ROUGE-L changes little. The effect shrinks as
  k grows (8 > 32 > 128).
- **H2 (tail tokens; exploratory for PopQA).** The gap between top-k and full KD is largest on gold
  tokens the teacher ranks outside its top-8 and on rare tokens (training-frequency buckets < 100).
  PopQA accuracy by popularity bucket (teacher-correct questions only) is **exploratory** in the course
  paper and is reported even if null.
- **H3 (interaction).** The top-k vs full-KD diversity gap is larger under forward KL than under reverse
  KL. Top-k RKL renormalises both distributions on the top-k set T, so it gives no direct gradient to
  the student's tail mass: this is a deliberate contrast, not a fix.
- **H4 (question, not a claim).** Does the existing ghost-token / tail-bucket correction (Anshumann
  et al., 2025; TAD; TA-OPD) also recover diversity and tail coverage? As of 17 Oct 2026 none of these
  papers measures output diversity (checked; see LITERATURE.md).

Primary metrics: semantic diversity (8 samples, temperature 1, no top-k/top-p), distinct-2 (and its
first-32-words version), student/teacher entropy ratio, tail coverage outside the teacher top-8, and
next-token ECE. Secondary: ROUGE-L (greedy and sampled), Self-BLEU, precision/recall cross
log-likelihoods, within-tail conditional KL, token-frequency buckets, PopQA.

Reporting: mean ± std over seeds for every number; every run reported, including failed ones; no
claim of a difference whose seed spread overlaps (paired comparisons by seed and prompt in the analysis).

## Changes (appended; the hypotheses above are unchanged)
- **2026-10-18 (authors), after seed 1 of the main grid:** base-initialised reverse-KL students do not
  terminate, so H3 (top-k vs full gap larger under forward KL than under reverse KL) is tested on students
  initialised from the same-seed SFT model ("MiniLLM-style init"): top-8, top-128, tail-bucket-8 and full KD
  with reverse KL, plus the existing SFT-init top-8 and full KD with forward KL; seeds {1, 2}. Base-init RKL
  results are reported as a documented failure.
- **2026-10-18 (authors), later the same day:** the H3 test from SFT init is a matched 2 x 4 design: forward
  and reverse KL x top-8, top-128, tail bucket (k=8) and full KD, seeds {1, 2}. The post-hoc "tail amount
  vs tail identity" analysis of the tail bucket (from seed 1) is treated as exploratory and must be confirmed
  on seeds 2 and 3 independently.
- **2026-10-19 09:13 (authors):** paper endpoints declared after seeing seeds 1-2 (see EXPERIMENT_LOG.md):
  main contrast top-8 vs full KD (FKL); primary endpoints semantic diversity, Self-BLEU, ECE, rare-token
  loss, ROUGE-L. The original primary list above stays and is reported too.

## Temperature-sweep analysis plan (authors, 19 Oct 2026; written and committed BEFORE any sweep point was run)
Known when this was written: the T = 1.0 contrasts only (top-8 vs full KD, FKL, Dolly test: sampled ROUGE-L
14.6 vs 13.1, teacher log-likelihood of samples -2.34 vs -3.15, semantic diversity .317 vs .369). No other
temperature has been generated. Question: does top-8 lose diversity at EQUAL sample quality, or does it sit
on the same quality-diversity frontier as full KD (just shifted toward sharper samples)?

**Design.** FKL, training seed 1, Dolly test (500 prompts), 8 samples per prompt, sampling seed 0, no
top-k/top-p. Top-8: T in {1.0, 1.15, 1.3, 1.5}. Full KD: T in {0.7, 0.85, 1.0, 1.15}. If time allows (run after
the core points): RS-KD and tail bucket (k = 8) at T in {1.0, 1.15}. Per (arm, T): sampled ROUGE-L, semantic
diversity, Self-BLEU, finish rate, mean length, empty rate, and the reference-free quality measure
**teacher log-likelihood per token of the student's samples**. T = 1.0 is regenerated inside the sweep (same
code path) and must reproduce the main evaluation within sampling noise.

**Quality axes.** Primary: teacher log-likelihood per token (reference-free; sampled ROUGE-L against one
reference penalises diversity by construction). Secondary: sampled ROUGE-L. Both reward sharpness to some
degree, so a claim needs BOTH axes to agree.

**Read-off.** Interpolate full KD's curve linearly on adjacent-temperature segments to find its diversity at
the quality of each top-8 point; difference D = diversity(top-8) - diversity(full KD at equal quality). The
summary statistic is the mean of D over the top-8 points whose point-estimate quality lies strictly inside
full KD's measured quality range (set J, fixed from the point estimates). Uncertainty: paired prompt-level
bootstrap (10,000 resamples of the 500 prompts, the same resample for every arm and temperature, curves and
interpolation recomputed in every replicate). One training seed: the interval covers prompt and sampling
noise, NOT training-seed variance (context: training-seed sd of semantic diversity <= .005).

**Decision rule (primary diversity metric = semantic diversity, margin 0.01, chosen now: about 1/5 of the .053
gap at T = 1.0 and above the ~.004 seed spread). For each quality axis:**
- **Outcome A, "on the curve":** the 95% interval of the mean D lies inside (-0.01, +0.01).
  -> report: *"top-k shifts the student along the frontier (sharper), largely recoverable by temperature."*
- **Outcome B, "below":** the upper 95% limit of the mean D is below -0.01.
  -> report: *"top-k loses diversity at equal quality."*
- **Outcome C, "above":** the lower 95% limit is above +0.01 (top-8 MORE diverse at equal quality): reported
  as such, not as support for the hypothesis.
- **Inconclusive:** the interval straddles a margin: report both frontiers, make no headline claim.
- **Not determined:** J is empty (no overlap in quality). Pre-specified fallback: add temperatures (top-8 up
  to 1.8, full KD down to 0.5), log the change, state it in the paper.
Overall: A or B is claimed only if both quality axes give the same outcome; otherwise "mixed", reported as
such. Self-BLEU (margin +-1.0, lower = more diverse) is read the same way as a secondary check and shown, but
does not decide. Whatever the outcome, the paper reports the full curves, the interval, the seed-level caveat
and that this analysis was specified before the points were run.

### Added 19 Oct 2026 after the code review of the sweep analysis, BEFORE any sweep point was run (no tempsweep file existed)
These make the registered statistic and its failure modes explicit; the decision rule and margins above are unchanged.
- **Registered statistic requires every J point.** J is fixed from the point estimates (strictly inside full KD's
  quality range). A bootstrap replicate counts only if EVERY point in J has an equal-quality read-off (complete-J
  replicates); replicates are never averaged over a varying subset. The interval is therefore conditional on
  complete-J replicates. If fewer than 90% of replicates are complete, the overlap is not stable under
  resampling and the outcome is "not determined" (fallback: add temperatures).
- **Confirmatory output needs the complete registered core grid** (top-8 and full KD, all 8 temperatures, every
  artifact verified). Otherwise all output is labelled PROVISIONAL and no decision is issued.
- **Artifacts are verified:** each per-prompt file carries its setup and is bound to its result row by sha256;
  500 unique prompt ids, every array of length 500, n_samples 8, max_new_tokens 256, sampling seed 0.
- **Quality definition.** teacher_lp = per-prompt mean (over the 8 samples) of each sample's mean token
  log-likelihood, averaged over prompts (the analysed quantity). The sample-weighted version (= the main
  evaluation's precision_teacher_lp, used for the T = 1.0 reproduction check) and the token-weighted version
  (sensitivity analysis, never decides) are also reported.
- **Missing scores.** NaN and +-inf are missing; every mean is over finite values; counts per point are printed
  and flagged above 2%.
- **Crossings.** First geometric crossing in increasing temperature; if its diversity is undefined the result is
  undefined (no fallback to a later crossing); multiple crossings are counted and reported.
- **Scope of outcome A.** It supports equivalence of the registered average matched-quality diversity for this
  checkpoint pair and one training seed; it does not establish that temperature recovers the distribution.
- **Reporting additions (19 Oct, still before any sweep point; they do not change the decision):** the decision
  block also prints the Self-BLEU outcomes (secondary, margin 1.0) and the token-weighted-teacher-lp sensitivity
  outcome with a consistency flag; every top-8 point is listed with its linear and PCHIP read-off (a chord
  underestimates a concave curve, which biases D upward: the PCHIP column is the check); notes are printed when J
  has a single point or when more than 5% of replicates have multiple crossings; empty-sample rate and
  length/finish rate are printed per point.
