"""Decision logic of analysis/frontier.py on synthetic sweeps with a known answer (CPU, a minute).
Full KD lies on a line (diversity = 0.30 - 0.1*(lp + 2.2)); top-8 points are placed relative to it. Covers the
pre-specified outcomes (A, B, C, not determined, mixed axes) and the review fixes: artifact verification, the
gate on the complete registered grid (PROVISIONAL), unstable overlap, vertex crossing counts, undefined
diversity at the first crossing. Run: python tests/test_frontier.py"""
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "analysis"))
sys.path.insert(0, str(REPO))
REF, TOP = "full_fkl_135m_nall_s1_v1", "topk_k8_fkl_135m_nall_s1_v1"
IDS = [f"dolly_{i}" for i in range(500)]
FULL_T, FULL_LP = (0.7, 0.85, 1.0, 1.15), (-2.2, -2.6, -3.0, -3.4)
TOP_T = (1.0, 1.15, 1.3, 1.5)


def make_root(top_shift=0.0, top_q=(-1.9, -2.3, -2.7, -3.1), top_rouge_offset=0.0, drop=(), corrupt=None) -> Path:
    """top_shift: diversity offset of the top-8 points from the full-KD line. drop: (run, T) points to omit.
    corrupt: (run, T) whose per-prompt file is altered AFTER its hash was recorded."""
    root = Path(tempfile.mkdtemp(prefix="frontier_"))
    (root / "runs" / "sweep_perprompt").mkdir(parents=True)
    rng = np.random.default_rng(1)
    rows = []

    def noise(sd):  # exactly mean-zero, so the point estimates equal the nominal values
        x = rng.normal(0, sd, 500)
        return x - x.mean()

    def add(run, T, lp, shift, rouge_offset=0.0):
        if (run, T) in drop:
            return
        rouge = 14 + 2.5 * (lp + 3.0) + rouge_offset
        div = 0.30 - 0.1 * (lp + 2.2) + shift
        setup = {"run_id": run, "split": "test", "temperature": T, "seed": 0, "n_prompts": 500, "n_samples": 8,
                 "max_new_tokens": 256, "config_version": "v1"}
        lp_arr = lp + noise(0.3)
        pp = {"setup": setup, "ids": IDS, "rougeL_sampled": list(rouge + noise(1.0)),
              "semantic_div": list(div + noise(0.05)),
              "self_bleu": list(12 - 20 * (div - 0.3) + noise(2.0)),
              "teacher_lp": list(lp_arr), "teacher_lp_tok": list(lp_arr + 0.01), "len": [170.0] * 500,
              "ended": [0.6] * 500}
        f = root / "runs" / "sweep_perprompt" / f"{run}__test__T{T:g}.json"
        raw = json.dumps(pp).encode()
        f.write_bytes(raw)
        sha = hashlib.sha256(raw).hexdigest()
        if corrupt == (run, T):
            f.write_bytes(raw.replace(b"dolly_0", b"dolly_9", 1))
        rows.append({**setup, "rougeL_sampled": rouge, "semantic_div": div, "self_bleu": 12.0, "teacher_lp_seq": lp,
                     "teacher_lp_prompt": lp, "teacher_lp_tok": lp, "ended_rate_sampled": 0.6, "len_sampled": 170.0,
                     "n_missing_lp": 0, "n_prompts_missing_semantic": 0, "n_prompts_missing_self_bleu": 0,
                     "perprompt_sha256": sha})

    for T, lp in zip(FULL_T, FULL_LP):
        add(REF, T, lp, 0.0)
    for T, lp in zip(TOP_T, top_q):
        add(TOP, T, lp, top_shift, top_rouge_offset)
    (root / "runs" / "tempsweep.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return root


def run(root: Path) -> str:
    out = subprocess.run([sys.executable, str(REPO / "analysis" / "frontier.py")], capture_output=True, text=True,
                         env={"CF_ROOT": str(root), "PATH": "", "SYSTEMROOT": "C:\\Windows"}, cwd=REPO)
    assert out.returncode == 0, out.stderr[-1500:]
    return out.stdout


def decision(stdout: str) -> str:
    line = [ln for ln in stdout.split("\n") if ln.startswith("DECISION")][0]
    return line.split("): ")[1].split(" -> ")[0]


def test_on_curve():
    out = run(make_root(0.0))
    assert decision(out) == "A", out
    assert "PROVISIONAL" not in out


def test_below():
    assert decision(run(make_root(-0.05))) == "B"


def test_above():
    assert decision(run(make_root(+0.05))) == "C"


def test_no_overlap():
    out = run(make_root(0.0, top_q=(-1.2, -1.5, -1.7, -1.9)))  # all above full KD's best quality
    assert decision(out) == "not determined", out


def test_axes_disagree_is_mixed():
    out = run(make_root(0.0, top_rouge_offset=2.0))
    assert decision(out) == "inconclusive" and "disagree" in out, out


def test_unstable_overlap_is_not_determined():
    # one top-8 point sits just inside full KD's best quality: in many replicates it falls outside the range, so
    # the registered statistic is not defined for them -> "not determined", not an interval from the lucky replicates
    out = run(make_root(0.0, top_q=(-2.21, -2.3, -2.7, -3.1)))
    assert decision(out) == "not determined" and "overlap not stable" in out, out


def test_incomplete_grid_is_provisional():
    out = run(make_root(0.0, drop=[(REF, 0.85)]))
    assert "PROVISIONAL: the registered core grid" in out and "full KD T=0.85" in out
    assert not any(ln.startswith("DECISION") for ln in out.split("\n")), out


def test_tampered_artifact_is_rejected():
    out = run(make_root(0.0, corrupt=(TOP, 1.15)))
    assert "INVALID point top-8 cache T=1.15: per-prompt file does not match the recorded sha256" in out, out
    assert "PROVISIONAL" in out and not any(ln.startswith("DECISION") for ln in out.split("\n"))


def test_interp_first_vertex_and_nan():
    import frontier as F
    qf, df = np.array([[0.0, 1.0, 2.0]]), np.array([[0.0, 0.5, 1.0]])
    v, c = F.interp_first(qf, df, np.array([1.0]))  # exactly at a shared vertex: one crossing
    assert c[0] == 1 and abs(v[0] - 0.5) < 1e-12
    v, c = F.interp_first(qf, df, np.array([0.5]))
    assert c[0] == 1 and abs(v[0] - 0.25) < 1e-12
    # non-monotone curve: quality 0 -> 2 -> 1; target 1.5 is crossed twice (up, then down): first crossing wins
    qf2, df2 = np.array([[0.0, 2.0, 1.0]]), np.array([[0.0, 1.0, 0.4]])
    v, c = F.interp_first(qf2, df2, np.array([1.5]))
    assert c[0] == 2 and abs(v[0] - 0.75) < 1e-12
    # undefined diversity at the first crossing stays undefined: no fallback to the later crossing
    df3 = np.array([[np.nan, 1.0, 0.4]])
    v, c = F.interp_first(qf2, df3, np.array([1.5]))
    assert c[0] == 2 and np.isnan(v[0])
    # outside the range: no crossing
    v, c = F.interp_first(qf, df, np.array([2.5]))
    assert c[0] == 0 and np.isnan(v[0])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("ALL FRONTIER TESTS PASSED")
