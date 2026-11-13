"""audit/score_audit.py on a synthetic, fully rated sheet with a known answer (CPU, seconds).
Run: python tests/test_audit.py"""
import csv
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "audit"))
import score_audit as S  # noqa: E402


def test_known_difference_and_perfect_agreement():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "audit").mkdir()
    S.HERE, S.REPO = tmp / "audit", tmp
    rng = np.random.default_rng(0)
    rows, keys = [], []
    n = 0
    for p in range(50):
        raters = "both" if p < 20 else ("r1" if p < 35 else "r2")
        for c in ("top8", "full"):
            for j in range(2):
                n += 1
                iid = f"R{n:03d}"
                fol = 3 if c == "full" else 2          # full KD follows better by exactly 1 point
                cor = "yes" if c == "full" else "no"   # correctness 100% vs 0%
                coh = 3
                r = {"item_id": iid, "raters": raters, "instruction": "i", "context": "", "reference_answer": "a",
                     "response": "x", "notes": ""}
                for who in ("r1", "r2"):
                    filled = raters == "both" or raters == who
                    r[f"{who}_follows_1to3"] = fol if filled else ""
                    r[f"{who}_correct_yes_no_na"] = cor if filled else ""
                    r[f"{who}_coherent_1to3"] = coh if filled else ""
                rows.append(r)
                keys.append({"item_id": iid, "prompt_id": f"p{p}", "condition": c, "run_id": "x",
                             "temperature": 1.3 if c == "top8" else 1.0, "sample_index": j})
    order = rng.permutation(len(rows))
    with open(tmp / "audit" / "rating_sheet.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows([rows[i] for i in order])
    with open(tmp / "audit" / "key.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(keys[0]))
        w.writeheader()
        w.writerows(keys)
    assert S.main() == 0
    tex = (tmp / "tables" / "audit.tex").read_text(encoding="utf-8")
    assert "-1.00 [-1.00, -1.00]" in tex, tex          # follows: top-8 minus full = -1
    assert "-100.0 [-100.0, -100.0]" in tex, tex        # correct: 0% vs 100%
    assert "100\\% (1.00)" in tex, tex                   # perfect agreement on the follows scale


if __name__ == "__main__":
    test_known_difference_and_perfect_agreement()
    print("ALL AUDIT TESTS PASSED")
