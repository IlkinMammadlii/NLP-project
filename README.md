# Does the Cache Forget? Top-k Teacher Caching Sharpens Distilled Students but Loses Tail Identity

Sabuhi Aghayev and Ilkin Mammadli. Report: [paper.pdf](paper.pdf) (ACL format), [course_paper.pdf](course_paper.pdf),
and the detailed report in [presentation/report/](presentation/report/). A short, commented, single-file version of
the training code: [presentation/distill_clean.py](presentation/distill_clean.py). Qualitative examples:
[presentation/EXAMPLES.md](presentation/EXAMPLES.md).

Does caching only the teacher's top-k probabilities make distilled students less diverse and worse at
long-tail knowledge? Literature check: [LITERATURE.md](LITERATURE.md). Results: [RESULTS.md](RESULTS.md).

Teacher SmolLM2-1.7B-Instruct → students SmolLM2-135M / 360M on databricks-dolly-15k (MiniLLM protocol),
one RTX 4060 (8 GB).

## Setup (Windows, Git Bash; Python 3.11)
```bash
py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install torch==2.11.0 --index-url https://download.pytorch.org/whl/cu128
.venv/Scripts/python.exe -m pip install -r requirements.txt      # exact versions: requirements.lock.txt
.venv/Scripts/python.exe -c "import torch; print(torch.cuda.get_device_name(0), torch.cuda.is_bf16_supported())"
```

## Tests (CPU, no downloads, ~1 min). Run after every code change.
```bash
.venv/Scripts/python.exe tests/test_losses.py      # ALL LOSS TESTS PASSED
.venv/Scripts/python.exe tests/test_losses_tiny.py # every loss vs hand formulas on a 4-token vocabulary
.venv/Scripts/python.exe tests/test_pipeline.py    # ALL PIPELINE TESTS PASSED
.venv/Scripts/python.exe tests/test_metrics.py     # ALL METRIC TESTS PASSED
```

## Run
```bash
.venv/Scripts/python.exe cf.py data            # splits -> cf_data/data (11,317 / 1,000 / 500)
.venv/Scripts/python.exe cf.py pilot           # Day 1 gate: SFT, full KD, top-8 (FKL & RKL), 2k examples, valid split
.venv/Scripts/python.exe cf.py sweep           # dev LR choice (valid ROUGE-L), then freeze it in cachekd/protocol.py
.venv/Scripts/python.exe cf.py popqa-teacher   # teacher answers on PopQA (exploratory long-tail probe)
.venv/Scripts/python.exe cf.py cache           # Day 2: one teacher pass: top-128 probs/ids, tail mass, RS draws
.venv/Scripts/python.exe cf.py seqkd           # teacher greedy responses for the seq-KD arm
.venv/Scripts/python.exe cf.py train --arm tailbucket --k 8 --div fkl --seed 1
.venv/Scripts/python.exe cf.py eval --run tailbucket_k8_fkl_135m_nall_s1_v1 --split valid
.venv/Scripts/python.exe cf.py grid --with-ablation   # the frozen main grid: train + eval every arm x seed
.venv/Scripts/python.exe cf.py table --split test     # mean ± std over seeds
```
Everything is resumable and skips finished runs. Outputs go to `$CF_ROOT` (default `cf_data/`).

| Arm (`--arm`) | Student sees | Source |
|---|---|---|
| `sft` | gold Dolly responses (CE) | – |
| `seqkd` | teacher greedy responses (CE) | `cf.py seqkd` |
| `topk --k 8/32/128` | renormalised teacher top-k | cache (or `--source online`) |
| `tailbucket --k K` | top-k + leftover tail mass, (k+1)-way (prior work: TAD, TA-OPD) | cache |
| `rs` | Random Sampling KD, 50 draws/position (Anshumann et al. 2025) | cache |
| `full` | full teacher distribution | online (teacher in memory) |

Each KD arm takes `--div fkl` or `--div rkl`.

## Decisions (authors, 17 Oct 2026)
1. Tail bucket = prior work (Anshumann et al.'s ghost token, TAD, TA-OPD): a baseline. H4 is now a
   question: does the ghost-token correction also recover diversity and tail coverage? (None of the three
   papers measures diversity; checked 17 Oct.)
2. LR: `cf.py sweep` over {1e-4, 2e-4, 3e-4} on SFT and full-KD FKL at n=2k, pick by validation ROUGE-L,
   freeze one shared LR in `cachekd/protocol.py`.
3. Pure KD is the default; KD+CE (0.5) ablation on top-8 and full KD, FKL, 2 seeds if time (`grid --with-ablation`).
4. Top-k RKL is kept as a deliberate contrast (no gradient to the tail mass), at k = 8 and 128 only.
5. RS-KD with FKL only (the CLI refuses `--arm rs --div rkl`).
6. PopQA is exploratory in the course paper; a fact-independent token-frequency tail metric was added.
7. No on-policy arm in the course paper. Eval sets: Dolly test, then Self-Instruct, then Vicuna (S-NI if time).

The frozen protocol is in `cachekd/protocol.py`; hypotheses in [HYPOTHESES.md](HYPOTHESES.md).
