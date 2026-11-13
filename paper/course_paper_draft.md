# What the Cache Forgets: Teacher-Logit Truncation and Homogenization in LLM Distillation

*Sabuhi Aghayev, Ilkin Mammadli* — course paper draft, 19 Oct 2026.
Status: Sections 1–4 drafted; Section 5 uses **seeds 1–2 as placeholders** (marked ⟨2-seed⟩); final numbers
are means ± std over seeds 1–3 once the grid finishes. Every number must match `cf.py table` /
`FINDINGS.md`. Convert to the ACL LaTeX template before submission.

---

## Abstract (placeholder; write last)

Distilling a large language model into a small one on limited compute almost always means caching the
teacher's outputs, and because full-vocabulary distributions are too large to store, practitioners keep only
the top-k probabilities. We ask what this routine engineering choice does to the student beyond accuracy.
Distilling SmolLM2-1.7B-Instruct into SmolLM2-135M on Dolly with seven teacher-signal formats and two
divergences, we find that top-k truncation reduces sampled diversity despite similar greedy ROUGE-L: with
forward KL, top-8 caching lowers semantic diversity by ⟨14%⟩ and nearly doubles next-token calibration error
relative to full distillation. Its samples are, however, scored higher by sampled ROUGE-L and by the
teacher's likelihood, so truncation trades diversity for sharper samples ⟨temperature-sweep result: whether
top-8 is still less diverse at equal sampled quality⟩. The effect shrinks monotonically with k. The existing
"ghost token" correction closes about three quarters of the gap in tail probability mass but only about half
of the gap in within-tail KL and rare-token loss, whereas random-sampling distillation closes over 90% of
both. ⟨Reverse-KL result from SFT
init.⟩ ⟨PopQA null.⟩

## 1 Introduction

Knowledge distillation (KD) trains a small *student* to imitate a larger *teacher* (Hinton et al., 2015).
For language models, token-level KD matches the student's next-token distribution to the teacher's at every
position of a training sequence. When compute is limited, the teacher cannot run next to the student for
every step, so its outputs are computed once and **cached**. A full distribution over a 49k-token
vocabulary is too large to store for millions of positions, so practitioners keep only the teacher's top-k
probabilities and renormalize them, choosing k by convenience (e.g. k = 100 in arXiv 2608.03796; k from 16
to 4,096 in arXiv 2610.15589).

Prior work established that this truncation is **biased**: renormalized top-k targets are not an unbiased
estimate of the teacher (Anshumann et al., 2025) and give no gradient to the student's probability mass
outside the cached set, so that mass drifts (arXiv 2610.15589). These works measure loss, accuracy and
calibration. What the student *generates* has not been measured. A student trained to put all its mass on
the teacher's top-k tokens could become systematically sharper: less diverse in what it writes, less
faithful to the teacher's uncertainty, and worse on rare tokens that live in the teacher's tail. If so,
top-k caching is a hidden source of the output **homogenization** that recent work attributes to alignment
and distillation (e.g. *Quantification of LLM Distillation*, ACL 2025).

We study this directly. We distil SmolLM2-1.7B-Instruct into SmolLM2-135M on databricks-dolly-15k with
seven teacher-signal formats — ground-truth SFT, sequence-level KD, renormalized top-k (k = 8, 32, 128),
top-k plus a tail bucket ("ghost token"), random-sampling KD, and full-vocabulary KD — each with forward
and reverse KL where defined, three seeds, identical data order, steps and learning rate. We measure
quality (ROUGE-L), lexical and semantic diversity of eight samples per prompt, calibration, coverage of the
teacher's tail, and loss on rare tokens.

Our contributions:
1. **A measurement of what top-k caching changes in generation.** Top-8 caching reduces sampled
   diversity and worsens calibration despite similar greedy ROUGE-L, with a monotone dose–response in k
   (§5.1); it survives controls for answer length (§5.2). Sampled quality is not matched (top-8 samples are
   sharper), so we map the quality–diversity frontier across sampling temperature (§5.5).
2. **Tail amount vs tail identity.** The ghost-token correction closes ~74% of the top-8→full gap in the
   student's probability mass outside the cached set (tail amount), but only ~52% of the gap in within-tail
   KL and rare-token loss (tail identity) and ~62% of the gap in semantic diversity; random-sampling KD
   closes 92–98% on all of them (§5.3). We show why with an exact decomposition of the KL (§3.3).
3. **The divergence interaction.** ⟨Reverse KL from SFT init; base-init RKL fails to terminate (§5.4).⟩
4. **Open, reproducible code on one 8 GB GPU,** including a pre-registered protocol, every run logged
   (including failures), and length-controlled diversity metrics.

## 2 Background and related work

**Token-level KD and the choice of divergence.** Forward KL, KL(p‖q), is mass-covering; reverse KL,
KL(q‖p), is mode-seeking and was proposed for LLM distillation by MiniLLM (Gu et al., 2024); DistiLLM
(Ko et al., 2024) uses a skewed KL. Several works report that reverse KL reduces output diversity and add
forward-KL terms or modify the gradient to counter it (EOPD, Jin et al., 2026; DRKL, Luong et al., 2026).
These studies vary the divergence; we vary the *format of the cached teacher signal* and measure its
interaction with the divergence.

**Sparse and cached-logit KD.** Anshumann et al. (2025) show that caching top-k probabilities biases the
teacher estimate and harms calibration, and propose Random Sampling KD (RS-KD): draw tokens from the
teacher and train on their empirical frequencies, which is unbiased in expectation. They also define a
"ghost token" that collects the teacher's leftover mass as one extra class. The same (k+1)-way construction
appears in TAD (Dasgupta et al., 2026), which adds a re-weighted tail term, and in TA-OPD (Huang & Wei,
2026) for on-policy reverse KL. A recent analysis proves that renormalized top-k KD gives zero gradient
along tail-controlling directions and shows that the student's tail mass drifts (arXiv 2610.15589).
**None of these works measures output diversity, tail coverage of generations, or long-tail behaviour;**
this is the gap we address. We use the ghost token and RS-KD as baselines, not as our contributions.

**Homogenization and long-tail knowledge.** Distillation reduces diversity across models (*Quantification
of LLM Distillation*, ACL 2025), and long-tail facts are learned poorly (Kandpal et al., 2023; Mallen et al.,
2023, PopQA). We probe both token-level tail behaviour and factual recall by entity popularity.

## 3 Method

Let x be a prompt, y a response, and at each response position t let p = p(·|x, y<t) be the teacher's and
q = q_θ(·|x, y<t) the student's next-token distribution over the vocabulary V (|V| = 49,152). Losses are
averaged over response tokens; all equations below are unit-tested against hand calculations on a 4-token
example (`tests/test_losses_tiny.py`).

### 3.1 Teacher-signal formats ("arms")

- **SFT:** cross-entropy on the gold response, −log q(y_t).
- **Seq-KD** (Kim & Rush, 2016): cross-entropy on the teacher's greedy response instead of the gold one.
- **Full KD:** the full teacher distribution, computed online with the teacher in memory.
  FKL: Σ_i p_i log(p_i/q_i); RKL: Σ_i q_i log(q_i/p_i).
- **Top-k** (k ∈ {8, 32, 128}): T = the teacher's k most probable tokens, p̂_i = p_i / p(T) for i ∈ T.
  FKL uses the student's full softmax: Σ_{i∈T} p̂_i log(p̂_i / q_i). This target puts zero mass outside T,
  so it pushes the student's tail toward zero. RKL renormalizes both distributions on T:
  KL(q_T/q(T) ‖ p̂); it is invariant to the student's tail logits and therefore gives **no gradient to the
  student's tail mass**. Both are the standard sparse forms.
- **Tail bucket / ghost token** (Anshumann et al., 2025; TAD; TA-OPD), k = 8: a (k+1)-way distribution
  p̃ = (p_T, 1 − p(T)), q̃ = (q_T, 1 − q(T)); FKL KL(p̃‖q̃), RKL KL(q̃‖p̃). It costs one extra float per
  position.
- **RS-KD** (Anshumann et al., 2025): N = 50 tokens drawn from the teacher at temperature 1;
  p̂_i = count_i / N; FKL KL(p̂ ‖ q) with the student's full softmax. (A support-restricted reverse KL is
  biased — a once-drawn token receives target mass ≥ 1/N — so RS-KD is run with forward KL only.)

### 3.2 Cache

One teacher pass over the training responses stores, per position, the top-128 probabilities (fp32), their
token ids, the exact leftover mass 1 − p(T) computed in log space, and the 50 RS-KD draws (870,417
positions, 1.0 GB). Smaller k are slices of the same cache, with the tail mass of rank k+1..128 added
back. Online and cached targets go through the same top-k procedure, so they differ only in floating
point.

### 3.3 Why a tail bucket restores amount but not identity

For any set T, the forward KL decomposes exactly as

KL(p‖q) = KL(p̃‖q̃) + p(V∖T) · KL( p(·|V∖T) ‖ q(·|V∖T) ),

where p(·|V∖T) is the teacher's distribution renormalized over the tail (verified numerically in our tests).
The tail bucket optimizes the first term only: it matches *how much* mass the student puts in the tail,
but leaves the student's distribution *within* the tail — which tokens get that mass — untrained. Plain
top-k FKL optimizes neither tail quantity. RS-KD's sampled targets include tail tokens individually and
estimate both terms. We therefore measure tail **amount** (student mass outside the teacher's top-8) and
tail **identity** (the within-tail KL above, and the loss on rare gold tokens) separately (§4.3). *This
split was chosen after seeing seed 1; seeds 2–3 serve as its confirmation.*

## 4 Experimental setup

### 4.1 Models and data

Teacher: SmolLM2-1.7B-Instruct (bf16, chat template). Students: SmolLM2-135M base. The two share one
49,152-token vocabulary, and the student sees the MiniLLM/Alpaca prompt while the teacher sees its chat
template; the same response token ids are appended to both, so every response position aligns. Data:
databricks-dolly-15k following MiniLLM's protocol (prompts over 256 tokens dropped; sequences cut at 512
tokens): 500 test and 1,000 validation examples, and 11,221 training examples after removing 96 training
items that share an instruction or a context passage with validation or test. Additional test sets:
Self-Instruct (242) and Vicuna (80) from MiniLLM's released files.

### 4.2 Training

Identical for every arm: AdamW (weight decay 0.01), learning rate 2e-4 chosen on the validation split by a
sweep over {1e-4, 2e-4, 3e-4} for SFT and full-KD FKL (2,000 examples), batch 16, 3 epochs (2,106 steps),
5% warm-up then linear decay, gradient clipping 1.0, fp32 weights with bf16 autocast, fp32 output layer,
gradient checkpointing, temperature 1, pure KD loss (no added cross-entropy). Seeds 1, 2, 3. One RTX 4060
laptop GPU (8 GB): cached arms ≈ 35 min, full KD ≈ 70 min of training per run, peak memory ≤ 7.5 GB.
Ablations: KD + 0.5·CE for top-8 and full FKL; KD from the same-seed SFT student ("MiniLLM-style init")
for top-8, top-128, tail bucket and full KD with both divergences, seeds 1–2; a sampling-temperature sweep
(top-8: T ∈ {1.0, 1.15, 1.3, 1.5}; full KD: T ∈ {0.7, 0.85, 1.0, 1.15}; RS-KD and tail bucket: T ∈ {1.0, 1.15};
FKL, seed 1) whose analysis plan and decision rule were fixed before the points were run (§5.5).

### 4.3 Evaluation

- **Quality:** ROUGE-L of the greedy response (and the mean over samples), MiniLLM normalization.
- **Diversity:** eight samples per prompt at temperature 1 with no top-k/top-p truncation (truncation
  would hide exactly the tail we study): semantic diversity (mean pairwise cosine distance of
  all-MiniLM-L6-v2 embeddings), Self-BLEU, distinct-2. Length-controlled versions use the first 32 words of
  every sample, or finished samples only.
- **Calibration and tail behaviour** (teacher-forced on 300 held-out gold responses): next-token ECE
  (15 bins), student/teacher entropy ratio, student mass outside the teacher's top-8 (tail amount),
  within-tail KL (tail identity), and the student's loss on rare gold tokens (fewer than 10 occurrences in the
  training responses and not present in the prompt).
- **Long-tail knowledge (exploratory):** PopQA accuracy by subject-popularity quintile on the 2,444
  questions the teacher answers correctly.
- **Endpoints.** Main contrast: top-8 vs full KD, forward KL, Dolly test. Primary endpoints (declared on 19
  Oct after seeing seeds 1–2, before seed 3 of the KD arms; the 17 Oct pre-registration listed semantic
  diversity, distinct-2, entropy ratio, tail coverage and ECE as primary): semantic diversity, Self-BLEU,
  ECE, rare-token loss, ROUGE-L. We report mean ± std over seeds.

## 5 Results (⟨2-seed placeholders⟩)

### 5.1 Top-k truncation reduces sampled diversity despite similar greedy ROUGE-L

⟨Table 1, Dolly test, forward KL, mean ± std over seeds 1–2:⟩

| | top-8 | top-32 | top-128 | tail bucket | RS-KD | full KD |
|---|---|---|---|---|---|---|
| greedy ROUGE-L | 18.6 | 18.6 | 18.7 | 18.6 | 18.6 | 18.8 |
| sampled ROUGE-L (mean of 8) | 14.6 | 13.9 | 13.5 | 13.6 | 13.2 | 13.1 |
| teacher log-lik of samples ↑ | −2.34 | −2.65 | −2.86 | −2.78 | −3.12 | −3.15 |
| semantic diversity ↑ | .316 ± .000 | .340 ± .004 | .351 ± .003 | .349 ± .002 | .368 ± .002 | .369 ± .001 |
| next-token ECE ↓ | .107 ± .002 | .076 ± .001 | .065 ± .000 | .069 ± .002 | .059 ± .001 | .057 ± .002 |
| rare-token loss ↓ | 5.32 | 5.03 | 4.88 | 5.02 | 4.78 | 4.75 |

Top-8 caching lowers semantic diversity by ⟨14%⟩ relative to full KD and nearly doubles ECE, while greedy
ROUGE-L differs by less than 0.3 points ⟨paired-bootstrap 90% CI within the ±1.0 equivalence margin⟩. Sampled
quality is not matched: top-8 samples score higher by sampled ROUGE-L (+1.5) and by the teacher's
likelihood, i.e. top-8 students are sharper; we therefore do not claim "matched quality" or "useful
diversity" (§5.5). Every metric orders top-8 < top-32 < top-128 ≈ tail bucket < RS-KD ≈
full. Seed-to-seed standard deviations (≤ .004) are an order of magnitude below the top-8 vs full gap.
⟨Self-BLEU, Self-Instruct, Vicuna. Figure 1: each metric vs k with full KD as a reference line.⟩

### 5.2 The effect is not an artefact of answer length

⟨Length-controlled table: first 32 words / finished only; ~80–85% of the gap remains.⟩

### 5.3 The ghost token restores tail amount, not tail identity

⟨Table 2: share of the top-8 → full gap closed, by metric (mean of seeds 1–2; RS-KD in brackets): tail
mass outside top-8 74% (98%); within-tail KL 52% (93%); rare-token loss 53% (95%); semantic diversity 62%
(98%); Self-BLEU 72% (92%); ECE 76% (95%). Per seed: amount 74% / 75%, identity 51–53% / 52–53%. Seed 3 =
confirmation.⟩

### 5.4 Forward vs reverse KL

⟨Base-init reverse-KL students do not terminate (≈1% of samples end): documented failure. H3 tested from
SFT init, 2 × 4 design — results pending.⟩

### 5.5 Temperature, CE and PopQA

⟨Temperature sweep (FKL, seed 1; temperatures as in §4.2): frontier of sample quality (teacher log-likelihood per
token, reference-free, primary; sampled ROUGE-L, secondary) vs semantic diversity / Self-BLEU (Figure 2). Key
question: at equal sample quality, is top-8 still less diverse? Pre-specified read-off by interpolation along
full KD's curve with a paired prompt-level bootstrap, margin 0.01 semantic diversity, both quality axes must
agree: ON THE CURVE → “top-k shifts the student along the frontier (sharper), largely recoverable by
temperature”; BELOW → “top-k loses diversity at equal quality”. Report honestly even if temperature closes the gap. Reference points: teacher, SFT, untrained base
student. PopQA: no effect of cache size (48.5–49.2%; base 19.7%, SFT 39.8%).⟩

### 5.6 Calibration in detail
⟨ECE with accuracy, NLL and Brier per arm and a reliability diagram (Figure 3); next-token accuracy is ~0.50
for every KD arm, so the ECE differences come from confidence, not accuracy.⟩

## Uncertainty
Differences between arms: prompt-level paired bootstrap (10,000 resamples over the 500 Dolly-test prompts)
per seed and pooled over seeds; seed-level spread reported separately as mean ± std; "no difference" in
greedy ROUGE-L only with the ±1.0-point equivalence margin and its 90% interval (two one-sided tests).

## 6 Limitations
- One model family and one student size; a second family (Qwen3) is planned.
- One learning rate, tuned on SFT and forward KL; reverse-KL arms may be disadvantaged.
- Primary endpoints were fixed after seeing seeds 1–2; the tail amount/identity split after seed 1.
- 135M students are more dispersed than the teacher, so higher diversity is not automatically better; we
  report closeness to the teacher alongside raw diversity. Sampled quality is not matched across arms, and
  whether the extra diversity is useful is not shown (a human evaluation and a task with several verifiable
  answers are planned for the conference version).
- Dolly contains few rare facts; the PopQA probe is exploratory.

## 7 Conclusion (placeholder)

## References (to complete)
Anshumann et al., 2025 (ACL) · Dasgupta, Cohn & Baldwin, 2026 (TAD, arXiv 2602.20816) · Huang & Wei, 2026
(TA-OPD, arXiv 2608.14728) · arXiv 2610.15589 (2026) · Gu et al., 2024 (MiniLLM) · Ko et al., 2024
(DistiLLM) · Jin et al., 2026 (EOPD, ICML) · Luong, Tran & Chen, 2026 (DRKL, arXiv 2604.00223) · Hinton et
al., 2015 · Kim & Rush, 2016 · Kandpal et al., 2023 · Mallen et al., 2023 · Quantification of LLM
Distillation, ACL 2025 · arXiv 2608.03796 (offline top-K KD, 2026).
