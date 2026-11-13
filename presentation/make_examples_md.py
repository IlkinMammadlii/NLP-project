"""Write presentation/EXAMPLES.md from presentation/examples.json (made by make_examples.py)."""
import json
from pathlib import Path

d = json.loads(Path("presentation/examples.json").read_text(encoding="utf-8"))
quote = lambda s: "\n".join("> " + line if line else ">" for line in s.strip().splitlines())  # noqa: E731
L = ["# Examples: teacher vs distilled students", "",
     "All prompts come from the 500-item Dolly test set, drawn at random (`random.Random(0)`); none was hand-picked.",
     "Every student is a 135M model trained on the same data with the same steps and learning rate. Only the teacher "
     "signal differs. Seed-1 runs.", "",
     f"- **Greedy** = the single most likely answer. **Samples** = {d['notes']['samples'].replace(' (cf_data/gens)', '')}.",
     "- Answers stop at 256 tokens, so some end mid-sentence.",
     "- Part 2 looks inside single next-word predictions where the gold word is in the teacher's tail.", "",
     "## Part 1: side-by-side answers", ""]
for n, g in enumerate(d["generations"], 1):
    L += [f"### {n}. {g['instruction'].strip()}", ""]
    if g["context"]:
        L += ["<details><summary>Context given with the prompt</summary>", "", quote(g["context"]), "", "</details>", ""]
    L += ["**Human reference (Dolly):**", "", quote(g["gold"]), ""]
    for m, o in g["outputs"].items():
        L += [f"**{m}, greedy:**", "", quote(o["greedy"]), ""]
    L += ["<details><summary>3 random samples per model</summary>", ""]
    for m, o in g["outputs"].items():
        for k, s in enumerate(o["samples"], 1):
            L += [f"**{m}, sample {k}:**", "", quote(s), ""]
    L += ["</details>", "", "---", ""]

M = ["Teacher", "Full KD", "Top-8 cache", "Tail bucket (k=8)", "RS-KD"]
pct = lambda p: f"{100 * p:.2f}%"  # noqa: E731
L += ["## Part 2: inside one prediction (the lost tail)", "",
      "Each case is one position in a real test answer, scored with the gold text as input. The gold next word is "
      "**outside the teacher's top 8**, so a top-8 cache never shows it to the student. Rows marked *tail* are the "
      "teacher's ranks 9-16: what a top-8 cache deletes.", "",
      f"Selection: {d['notes']['peek']}. Single positions only show what the effect looks like; the averages over all "
      "test tokens are in the report.", ""]
for n, p in enumerate(d["tail_peek"], 1):
    L += [f"### Case {n}: {p['instruction'].strip()[:100]}", "",
          f"`…{p['context_before'].strip()}` → gold next word **`{p['gold_token']}`** (teacher's #{p['gold_rank']} choice)", "",
          "| next word | teacher rank | " + " | ".join(M) + " |", "|---|---|" + "---:|" * len(M)]
    for t in p["tokens"]:
        tag = (" ◂ gold" if t["is_gold"] else "") + (" *(tail)*" if t["teacher_rank"] > 8 else "")
        word = json.dumps(t["tok"], ensure_ascii=False)[1:-1].replace("|", "\|")
        L.append(f"| `{word}`{tag} | {t['teacher_rank']} | "
                 + " | ".join(pct(t["p"][m]) for m in M) + " |")
    L += ["| **mass outside teacher's top 8** | | " + " | ".join(f"**{pct(p['mass_outside_teacher_top8'][m])}**" for m in M) + " |", ""]
Path("presentation/EXAMPLES.md").write_text("\n".join(L), encoding="utf-8")
print(len(L), "lines")
