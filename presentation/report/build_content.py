"""Turn the paper source (paper/acl/main.tex + tables/*.tex + macros) into report blocks (JSON) for the
detailed report, and add the report-only sections (implementation, compute, qualitative examples).

Every number in the paper part comes from tables/macros.tex and tables/*.tex (written by make_results.py);
the examples come from presentation/examples.json (make_examples.py). Nothing is typed by hand here except
the implementation/compute facts, each taken from the repo files named next to it.

Run from the repo root: .venv\\Scripts\\python.exe presentation\\report\\build_content.py
Writes presentation/report/content.json.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEX = (ROOT / "paper/acl/main.tex").read_text(encoding="utf-8")
MACROS = dict(re.findall(r"\\newcommand\{\\(\w+)\}\{(.*)\}", (ROOT / "tables/macros.tex").read_text(encoding="utf-8")))

# ---------------------------------------------------------------- bibliography (from the compiled .bbl)
BBL = (ROOT / "paper/acl/course_paper.bbl").read_text(encoding="utf-8")
CITE, REFS = {}, []
for m in re.finditer(r"\\bibitem\[\{(.+?)\((\d{4})\)(.*?)\}\]\{(\w+)\}(.*?)(?=\\bibitem|\\end\{thebibliography\})", BBL, re.S):
    short, year, _, key, body = m.groups()
    CITE[key] = (re.sub(r"\s+", " ", short.replace("et~al.", "et al.")).strip(), year)
    body = re.sub(r"\\href\s*\{[^}]*\}\s*", "", body)
    body = re.sub(r"\\newblock", " ", body)
    body = re.sub(r"\{\\'(\w)\}", lambda m: m.group(1) + "́", body)  # {\'i} -> í
    REFS.append(body)


# ---------------------------------------------------------------- inline LaTeX -> runs
GREEK = {"kappa": "κ", "tau": "τ", "theta": "θ", "alpha": "α"}
MATH_SYM = {r"\Vert": " ‖ ", r"\in": " ∈ ", r"\times": "×", r"\pm": "±", r"\ge": " ≥ ", r"\le": " ≤ ", r"\setminus": " ∖ ",
            r"\mid": " | ", r"\log": " log ", r"\sum": "Σ", r"\cdot": "·", r"\to": "→", r"\star": "*", r"\KL": "KL",
            r"\big": "", r"\Big": "", r"\left": "", r"\right": "", r"\quad": " ", r"\,": " ", r"\;": " ", r"\!": "",
            r"\ldots": "…", r"\dots": "…", r"\approx": " ≈ ", r"\Rightarrow": " ⇒ ", r"\rightarrow": " → "}


def math_runs(s: str) -> list:
    """$...$ -> runs with sub/superscripts (plain Unicode otherwise)."""
    s = s.replace("\\{", "\x01").replace("\\}", "\x02")  # literal braces of a set
    s = re.sub(r"\\(?:text|mathrm|textbf|operatorname)\{([^{}]*)\}", lambda m: m.group(1).replace(" ", "\x03"), s)
    for a, accent in (("hat", "\u0302"), ("tilde", "\u0303"), ("bar", "\u0304")):
        s = re.sub(r"\\%s\s*(?:\{(\w)\}|(\w))" % a, lambda m: (m.group(1) or m.group(2)) + accent, s)
    s = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1/\2)", s)
    s = re.sub(r"\\log\s*", "\\\\log", s)
    s = re.sub(r"(\\[A-Za-z]+)\s+", r"\1", s)  # a space after a command only ends the command
    s = re.sub(r"\s+", "", s).replace("\x03", " ")  # math mode ignores spaces
    for k, v in sorted(MATH_SYM.items(), key=lambda kv: -len(kv[0])):
        s = s.replace(k, v)
    s = re.sub(r"\\(\w+)", lambda m: GREEK.get(m.group(1), m.group(1)), s)
    s = s.replace("{,}", ",").replace("{=}", "=").replace("{+}", "+").replace("{-}", "-").replace("{\\textendash}", "–")
    runs, i, buf = [], 0, ""
    while i < len(s):
        c = s[i]
        if c in "_^" and i + 1 < len(s):
            if buf:
                runs.append({"t": buf, "i": True})
                buf = ""
            if s[i + 1] == "{":
                depth, j = 1, i + 2
                while j < len(s) and depth:
                    depth += {"{": 1, "}": -1}.get(s[j], 0)
                    j += 1
                inner, i = s[i + 2:j - 1], j
            else:
                inner, i = s[i + 1], i + 2
            runs.append({"t": inner.replace("{", "").replace("}", ""), "i": True, "sub" if c == "_" else "sup": True})
            continue
        if c not in "{}":
            buf += c
        i += 1
    if buf:
        runs.append({"t": buf, "i": True})
    for r in runs:
        r["t"] = r["t"].replace("\x01", "{").replace("\x02", "}")
    for r in runs:  # numbers and operators upright, letters italic
        if not re.search(r"[A-Za-z]", r["t"]) or r["t"].strip() in ("KL", "log", "ROUGE-L"):
            r.pop("i", None)
    return runs


LABELS = {}  # label -> "Section 4.2" / "Table 3" / ...


def cite(cmd: str, keys: str) -> str:
    items = [CITE[k.strip()] for k in keys.split(",")]
    if cmd == "citet":
        return "; ".join(f"{a} ({y})" for a, y in items)
    inner = "; ".join(f"{a}, {y}" for a, y in items)
    return inner if cmd == "citealp" else f"({inner})"


def expand(s: str) -> str:
    """Text-level replacements before splitting into runs."""
    for _ in range(3):
        s = re.sub(r"\\(\w+)\{\}", lambda m: MACROS.get(m.group(1), m.group(0)), s)
        s = re.sub(r"\\([A-Za-z]+)(?![A-Za-z{])", lambda m: MACROS.get(m.group(1), m.group(0)) if m.group(1) in MACROS else m.group(0), s)
    s = re.sub(r"\\(citep|citet|citealp)\{([^}]*)\}", lambda m: cite(m.group(1), m.group(2)), s)
    s = re.sub(r"(Table|Figure|Eq\.)~\\ref\{([^}]*)\}", lambda m: LABELS.get(m.group(2), "?").replace("Equation", "Eq."), s)
    s = re.sub(r"\\S\\ref\{([^}]*)\}", lambda m: LABELS.get(m.group(1), "?"), s)
    s = re.sub(r"\\ref\{([^}]*)\}", lambda m: LABELS.get(m.group(1), "?").split()[-1], s)
    s = re.sub(r"\\label\{[^}]*\}", "", s)
    s = s.replace("``", "“").replace("''", "”").replace("---", "—").replace("--", "–").replace("~", " ")
    s = s.replace("\\%", "%").replace("\\&", "&").replace("\\ ", " ").replace("\\,", " ").replace("\\@", "")
    s = s.replace(r"\_", "_")
    return s


def runs(s: str, bold=False, italic=False) -> list:
    """Inline LaTeX -> [{"t", "b", "i", "sub", "sup", "mono"}]."""
    s = expand(s)
    out, i = [], 0
    pat = re.compile(r"\\(emph|textbf|textit|texttt|url)\{|\$")
    while True:
        m = pat.search(s, i)
        if not m:
            break
        if m.start() > i:
            out.append({"t": s[i:m.start()], "b": bold, "i": italic})
        if m.group(0) == "$":
            j = s.index("$", m.end())
            out += [dict(r, b=bold, m=True) for r in math_runs(s[m.end():j])]
            i = j + 1
            continue
        depth, j = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(s[j], 0)
            j += 1
        inner = s[m.end():j - 1]
        cmd = m.group(1)
        if cmd in ("texttt", "url"):
            out.append({"t": inner, "mono": True, "b": bold})
        else:
            out += runs(inner, bold or cmd == "textbf", italic or cmd in ("emph", "textit"))
        i = j
    if i < len(s):
        out.append({"t": s[i:], "b": bold, "i": italic})
    for r in out:
        if not r.pop("m", False):  # math runs are already clean (and may hold literal set braces)
            r["t"] = re.sub(r"\s+", " ", r["t"].replace("{", "").replace("}", ""))
    return [r for r in out if r["t"]]


# ---------------------------------------------------------------- tables
def table_block(name: str) -> dict:
    src = (ROOT / "tables" / f"{name}.tex").read_text(encoding="utf-8")
    label = re.search(r"\\label\{([^}]*)\}", src).group(1)
    cap = re.search(r"\\caption\{(.*)\}\s*\\label", src, re.S).group(1)
    body = re.search(r"\\begin\{tabular\}\{[^}]*\}(.*)\\end\{tabular\}", src, re.S).group(1)
    body = re.sub(r"\\makecell\{(.*?)\}", lambda m: m.group(1).replace("-\\\\", "-").replace("\\\\", " "), body)
    head_src, rest = body.split("\\midrule", 1)
    # header lines (above the first \midrule); a \multicolumn spanning n columns is repeated n times and
    # the lines are joined per column ("Self-Instruct ROUGE-L")
    head_lines = [h.strip() for h in re.split(r"\\\\", head_src.replace("\\toprule", "")) if h.strip()]
    cols_per_line = []
    for h in head_lines:
        cols = []
        for c in re.split(r"(?<!\\)&", h):
            mc = re.match(r"\s*\\multicolumn\{(\d+)\}\{\w\}\{(.*)\}\s*$", c)
            cols += [mc.group(2)] * int(mc.group(1)) if mc else [c.strip()]
        cols_per_line.append(cols)
    header = [runs(" ".join(x for x in col if x)) for col in zip(*cols_per_line)]
    rows = []
    for line in re.split(r"\\\\\s*\n?", rest):
        line = re.sub(r"\\(toprule|midrule|bottomrule|cmidrule\S*)", "", line).strip()
        if not line:
            continue
        mc = re.match(r"\\multicolumn\{\d+\}\{\w\}\{(.*)\}\s*$", line, re.S)
        if mc:
            rows.append({"group": runs(mc.group(1))})
            continue
        rows.append({"cells": [runs(c.strip()) for c in re.split(r"(?<!\\)&", line)]})
    return {"type": "table", "label": label, "header": header, "rows": rows, "caption": runs(re.sub(r"\s+", " ", cap))}


# ---------------------------------------------------------------- first pass: numbering of labels
def number_labels(tex: str, extra_after: dict) -> None:
    tex = tex[tex.index("\\section{Introduction}"):]
    sec = [0, 0]
    tab = fig = eq = 0
    app = None  # index of the current appendix section
    for m in re.finditer(r"\\(section|subsection)\*?\{|\\label\{([^}]*)\}|\\input\{\\resultsdir tables/(\w+)\}|\\begin\{figure|\\begin\{equation|\\appendix", tex):
        tok = m.group(0)
        if tok.startswith("\\section{") and app is not None:
            app += 1
            sec = ["Appendix " + "ABCDEFG"[app], 0]
        elif tok.startswith("\\section{"):
            sec = [sec[0] + 1, 0]
            title = tex[m.end():tex.index("}", m.end())]
            sec[0] += extra_after.get(title, 0)
        elif tok.startswith("\\subsection"):
            sec[1] += 1
        elif tok.startswith("\\appendix"):
            app = -1
        elif m.group(3):
            tab += 1
            src = (ROOT / "tables" / f"{m.group(3)}.tex").read_text(encoding="utf-8")
            LABELS[re.search(r"\\label\{([^}]*)\}", src).group(1)] = f"Table {tab}"
        elif tok.startswith("\\begin{figure"):
            fig += 1
            lab = re.search(r"\\label\{([^}]*)\}", tex[m.end():tex.index("\\end{figure", m.end())]).group(1)
            LABELS[lab] = f"Figure {fig}"
        elif tok.startswith("\\begin{equation"):
            eq += 1
            lab = re.search(r"\\label\{([^}]*)\}", tex[m.end():tex.index("\\end{equation", m.end())]).group(1)
            LABELS[lab] = f"Equation {eq}"
        elif m.group(2) and m.group(2) not in LABELS:
            LABELS[m.group(2)] = (sec[0] if app is not None else f"Section {sec[0]}") + (f".{sec[1]}" if sec[1] else "")


# ---------------------------------------------------------------- second pass: blocks
FIG_PNG = {"dose": "figures/dose.png", "temperature": "figures/temperature.png",
           "reliability_test": "presentation/report/reliability_test.png"}


def body_blocks(tex: str) -> list:
    tex = tex[tex.index("\\section{Introduction}"):tex.index("\\end{document}")]
    tex = re.sub(r"(?m)^%.*$", "", tex)
    tex = re.sub(r"\\IfFileExists\{acl.sty\}\{\}\{\\bibliographystyle\{plainnat\}\}%?.*", "", tex)
    tex = tex.replace("\\bibliography{references}", "\\REFERENCES")
    blocks = []
    pos = 0
    tok = re.compile(r"\\section\*?\{([^}]*)\}(\\label\{[^}]*\})?|\\subsection\{([^}]*)\}(\\label\{[^}]*\})?|"
                     r"\\paragraph\{(.*?)\}|\\input\{\\resultsdir tables/(\w+)\}|\\begin\{(figure\*?|equation|itemize|enumerate)\}|"
                     r"\\appendix|\\REFERENCES|\n\s*\n")
    para = ""

    def flush():
        nonlocal para
        if para.strip():
            if blocks and blocks[-1]["type"] == "para" and blocks[-1].get("lead_open"):
                blocks[-1]["runs"] += runs(" " + para.strip())
                blocks[-1].pop("lead_open")
            else:
                blocks.append({"type": "para", "runs": runs(para.strip())})
        para = ""

    for m in tok.finditer(tex):
        para += tex[pos:m.start()]
        pos = m.end()
        g = m.group(0)
        if g.strip() == "":
            flush()
            continue
        flush()
        if m.group(1) is not None:
            blocks.append({"type": "h1", "text": plain(m.group(1)), "numbered": not g.startswith("\\section*")})
        elif m.group(3) is not None:
            blocks.append({"type": "h2", "text": plain(m.group(3))})
        elif m.group(5) is not None:
            blocks.append({"type": "para", "runs": runs(m.group(5).rstrip(".") + ". ", bold=True), "lead_open": True})
        elif m.group(6):
            blocks.append(table_block(m.group(6)))
        elif g == "\\appendix":
            blocks.append({"type": "appendix"})
        elif g == "\\REFERENCES":
            blocks.append({"type": "h1", "text": "References", "numbered": False})
            blocks += [{"type": "ref", "runs": runs(re.sub(r"\s+", " ", r).strip())} for r in REFS]
        else:
            env = m.group(7)
            end = tex.index("\\end{%s}" % env, pos)
            inner = tex[pos:end]
            pos = end + len("\\end{%s}" % env)
            if env.startswith("figure"):
                name = re.search(r"\\figorbox\{(\w+)\}", inner).group(1)
                cap = re.search(r"\\caption\{(.*)\}\\label\{([^}]*)\}", inner, re.S)
                blocks.append({"type": "figure", "png": FIG_PNG[name], "label": LABELS[cap.group(2)],
                               "caption": runs(re.sub(r"\s+", " ", cap.group(1))), "wide": env == "figure*"})
            elif env == "equation":
                lab = re.search(r"\\label\{([^}]*)\}", inner).group(1)
                eqs = re.sub(r"\\label\{[^}]*\}|\\begin\{aligned\}|\\end\{aligned\}|&", "", inner)
                eqs = " ".join(x.strip() for x in eqs.split("\\\\"))
                blocks.append({"type": "equation", "runs": math_runs(eqs), "label": LABELS[lab]})
            else:
                items = [x.strip() for x in re.split(r"\\item\s*", inner) if x.strip()]
                blocks.append({"type": "list", "ordered": env == "enumerate", "items": [runs(re.sub(r"\s+", " ", x)) for x in items]})
    para += tex[pos:]
    flush()
    return blocks


def plain(s: str) -> str:
    return "".join(r["t"] for r in runs(s))


def P(text, **kw):
    return {"type": "para", "runs": [{"t": text, **kw}]}


def rich(*parts):
    """rich(("bold text", "b"), "plain", ("mono", "mono"))"""
    out = []
    for p in parts:
        out.append({"t": p} if isinstance(p, str) else {"t": p[0], **{f: True for f in p[1].split(",")}})
    return {"type": "para", "runs": out}


def implementation_blocks() -> list:
    """Report-only section: how the code is organised and what one training run does."""
    res = [json.loads(l) for l in open(ROOT / "cf_data/runs/results.jsonl", encoding="utf-8")]
    ok = [r for r in res if r.get("saved") and r.get("n_train") == 11221]
    by = lambda arm: [r for r in ok if r["run_id"].startswith(arm)]  # noqa: E731
    med = lambda xs: sorted(xs)[len(xs) // 2]  # noqa: E731
    full_min = round(med([r["train_s"] for r in by("full_fkl_135m_nall")]) / 60)
    top_min = round(med([r["train_s"] for r in by("topk_k8_fkl_135m_nall")]) / 60)
    full_mem = max(r["peak_gpu_mem_gb"] for r in by("full_fkl_135m_nall"))
    top_mem = max(r["peak_gpu_mem_gb"] for r in by("topk_k8_fkl_135m_nall"))
    meta = json.loads((ROOT / "cf_data/cache/train/meta.json").read_text(encoding="utf-8"))
    n_pos = meta.get("positions") or meta.get("n_positions") or meta.get("P")
    cache_gb = sum(f.stat().st_size for f in (ROOT / "cf_data/cache/train").glob("*.npy")) / 1e9
    n_runs = len(ok)
    steps = [
        "Encode each training example twice: the teacher prompt (its chat template) and the student prompt (Alpaca/MiniLLM format), followed by the same answer token ids. The j-th answer position of both models therefore predicts the same token.",
        "Run the teacher once over all training answers and store, per answer position, its top-128 token ids and probabilities, the exact remaining tail mass (computed in log space) and 50 random draws. Smaller k is a slice of the same cache.",
        "For each optimizer step (batch 16, built from micro-batches), run the student, take its logits at the answer positions only, with the output layer in fp32.",
        "Build the target for the arm (full: live teacher logits; top-k: renormalised top-k; tail bucket: top-k plus one tail class; RS-KD: draw frequencies) and compute the per-position KL.",
        "Average the loss over all answer tokens of the optimizer batch, back-propagate, clip the gradient norm at 1.0, take an AdamW step with warm-up and linear decay.",
        "After 3 epochs save the student; evaluation then generates greedy and 8 sampled answers per prompt and scores them.",
    ]
    out = [{"type": "h1", "text": "Implementation", "numbered": True},
           P("The experiments were run with a Python pipeline driven by one command-line script. Every trained "
             "run, evaluation and failure was logged with its configuration, code version, run time and peak GPU "
             "memory."),
           {"type": "h2", "text": "What one training run does"},
           {"type": "list", "ordered": True, "items": [[{"t": s}] for s in steps]},
           rich("The script ", ("distill_clean.py", "mono"), ", submitted with this report,"
                " contains exactly these steps in one file, with comments. Its four loss functions were checked "
                "numerically against the loss code of that pipeline (identical values and gradients), and all four arms were "
                "trained end to end with it on the same GPU."),
           {"type": "h2", "text": "Compute"},
           {"type": "list", "ordered": False, "items": [
               [{"t": "Hardware: ", "b": True}, {"t": "one NVIDIA RTX 4060 Laptop GPU (8 GB)."}],
               [{"t": "Teacher cache: ", "b": True}, {"t": f"{n_pos:,} answer positions, {cache_gb:.2f} GB on disk "
                                                          f"(top-128 ids and probabilities, tail mass, teacher entropy, 50 RS draws holding on average {meta['rs_unique_mean']:.2f} different tokens per position). "
                                                          "The full distributions would be 171 GB."}] if n_pos else
               [{"t": "Teacher cache: ", "b": True}, {"t": f"{cache_gb:.2f} GB on disk; the full distributions would be 171 GB."}],
               [{"t": "Training time per run (median, full training set, 3 epochs): ", "b": True},
                {"t": f"top-8 from the cache {top_min} min (peak {top_mem} GB); full KD with the teacher live {full_min} min (peak {full_mem} GB)."}],
               [{"t": "Runs: ", "b": True}, {"t": f"{n_runs} completed student runs on the full training set, every one logged."}],
           ]},
           ]
    return out


def examples_blocks() -> list:
    """Report-only section: qualitative examples (prompts drawn at random, not hand-picked)."""
    d = json.loads((ROOT / "presentation/examples.json").read_text(encoding="utf-8"))
    show = ["Teacher (1.7B)", "Full KD", "Top-8 cache", "Tail bucket (k=8)", "RS-KD"]
    out = [{"type": "h1", "text": "Qualitative examples", "numbered": True},
           P("This section shows what the students actually write. The prompts are the first two of a fixed random draw "
             "from the 500-item Dolly test set (random.Random(0)); they were not chosen for how well they illustrate the "
             "results. All students are seed-1 runs trained with forward KL. Samples use temperature 1 with no top-k or "
             "top-p truncation. Answers stop at 256 tokens, so some end mid-sentence. Text is shown verbatim, including the models' own Markdown (**). The complete set (30 prompts, six "
             "students, greedy answers and three samples each) is in the file EXAMPLES.md, submitted with this report."),
           {"type": "h2", "text": "Side-by-side answers"}]
    for n, g in enumerate(d["generations"][:2], 1):
        out.append(rich((f"Prompt {n}: ", "b"), (g["instruction"].strip(), "b")))
        out.append({"type": "example", "who": "Human reference (Dolly)", "kind": "gold", "text": g["gold"].strip()})
        for m in show:
            out.append({"type": "example", "who": f"{m}, greedy", "kind": "teacher" if m.startswith("Teacher") else "student",
                        "text": g["outputs"][m]["greedy"].strip()})
        for m in ("Full KD", "Top-8 cache"):
            out.append({"type": "example", "who": f"{m}, sample 1 (T = 1)", "kind": "student",
                        "text": g["outputs"][m]["samples"][0].strip()})
    M = ["Teacher", "Full KD", "Top-8 cache", "Tail bucket (k=8)", "RS-KD"]
    out += [{"type": "h2", "text": "Inside one prediction: a word from the teacher's tail"},
            P("The tables below look at single positions of real test answers, scored with the gold answer as input. At "
              "each, the gold next word lies outside the teacher's top 8, so a top-8 cache never shows it to the student. "
              "Rows marked “tail” are the teacher's ranks 9–16, which a top-8 cache deletes. Selection: "
              + d["notes"]["peek"] + ". Single positions only illustrate the effect; the averaged measurements are in the "
              "Results section.")]
    for p in d["tail_peek"][:3]:
        out.append(rich(("Prompt: ", "b"), p["instruction"].strip()[:120], ("   Text so far: ", "b"),
                        "…" + p["context_before"].strip(), ("  →  gold next word: ", "b"), (p["gold_token"].strip(), "b,mono"),
                        f" (teacher's #{p['gold_rank']} choice)"))
        rows = []
        for t in p["tokens"]:
            word = t["tok"].replace("\n", "\\n").strip() or "␣"
            tag = (" ◂ gold" if t["is_gold"] else "") + (" (tail)" if t["teacher_rank"] > 8 else "")
            rows.append({"cells": [[{"t": word, "mono": True, "b": t["is_gold"]}, {"t": tag, "i": True}], [{"t": str(t["teacher_rank"])}]]
                         + [[{"t": f"{100 * t['p'][m]:.2f}%"}] for m in M], "tail": t["teacher_rank"] > 8})
        rows.append({"cells": [[{"t": "Mass outside the teacher's top 8", "b": True}], [{"t": ""}]]
                     + [[{"t": f"{100 * p['mass_outside_teacher_top8'][m]:.2f}%", "b": True}] for m in M]})
        out.append({"type": "table", "label": None, "numbered": False,
                    "header": [[{"t": "Next word", "b": True}], [{"t": "Teacher rank", "b": True}]] + [[{"t": m, "b": True}] for m in M],
                    "rows": rows, "caption": [{"t": "Probability each model gives to the teacher's top-16 words at this position."}]})
    return out


def summary_blocks() -> list:
    abstract = TEX[TEX.index("\\begin{abstract}") + 16:TEX.index("\\end{abstract}")]
    return [{"type": "h1", "text": "Summary", "numbered": False}, {"type": "para", "runs": runs(re.sub(r"\s+", " ", abstract.strip()))},
            {"type": "h2", "text": "Key findings"},
            {"type": "list", "ordered": False, "items": [
                runs(f"Top-8 caching versus full distillation (forward KL, Dolly test, 3 seeds): semantic diversity "
                     f"{MACROS['TopEightSemDiv']} vs {MACROS['FullSemDiv']} ({MACROS['SemDivDropPct']}% lower), ECE "
                     f"{MACROS['TopEightECE']} vs {MACROS['FullECE']} (×{MACROS['ECERatio']}), rare-token loss "
                     f"{MACROS['TopEightRare']} vs {MACROS['FullRare']}, greedy ROUGE-L difference {MACROS['DiffRougeG']} "
                     f"(90% interval {MACROS['GreedyCI']}, within the ±1.0 equivalence margin)."),
                runs("The effect grows as k shrinks (k = 128, 32, 8) on every metric."),
                runs(f"At matched reference overlap (pre-registered test) a residual diversity deficit is not supported; the "
                     f"formal verdict is {MACROS['HeadlineOutcome']}."),
                runs(f"The tail bucket restores {MACROS['RecTBAmount']}% of the tail amount but only {MACROS['RecTBWithinTail']}% "
                     f"of within-tail KL and {MACROS['RecTBRare']}% of rare-token loss; RS-KD restores {MACROS['RecRSAmount']}%, "
                     f"{MACROS['RecRSWithinTail']}% and {MACROS['RecRSRare']}%."),
            ]}]


# The report names only what its reader can open: the submitted files (distill_clean.py, EXAMPLES.md). The paper's
# pointers to repository files, log files, a commit hash and a code release are reworded here (authors, 13 Nov).
SUBMISSION_EDITS = [
    ("Open, reproducible code on one 8 GB GPU", "Experiments on one 8 GB GPU"),
    ("we fixed the following in the experiment log.", "we fixed the following in writing."),
    ("All data and models are public. Code, configurations, every run (including failed runs) and the pre-registration "
     "log are released with the paper.",
     "All data and models are public. The training code is submitted with this report (distill_clean.py)."),
    ("Every trained run, evaluation and failure is recorded in runs/results.jsonl, runs/evals.jsonl and "
     "runs/failed.jsonl; the experiment log lists every incident and decision with its date. The temperature selection "
     "and matched comparisons were pre-registered on 19 October 2026 before any of their runs (commit 57a44f4).",
     "Every trained run, evaluation and failure was logged with its date, and every incident and decision was written "
     "down when it happened. The temperature selection and matched comparisons were pre-registered on 19 October 2026, "
     "before any of their runs."),
]


def submission_edits(blocks: list) -> None:
    """Apply SUBMISSION_EDITS; a match that spans several runs collapses that paragraph to plain runs."""
    def fix(rs):
        for old, new in SUBMISSION_EDITS:
            for r in rs:
                if old in r["t"]:
                    r["t"] = r["t"].replace(old, new)
                    break
            else:
                joined = "".join(r["t"] for r in rs)
                if old in joined:
                    rs[:] = [{"t": joined.replace(old, new)}]
        return rs
    for b in blocks:
        if b["type"] == "para":
            fix(b["runs"])
        elif b["type"] == "list":
            for it in b["items"]:
                fix(it)
    text = " ".join(re.findall(r'"(?:t|text)": "((?:[^"\\]|\\.)*)"', json.dumps(blocks, ensure_ascii=False)))  # visible text only
    for bad in ("runs/", "57a44f4", "experiment log", "released with", "cachekd", "presentation/"):
        assert bad not in text, f"report still points to {bad!r}"


if __name__ == "__main__":
    # the report adds "Implementation" after Method and "Qualitative examples" after Results
    number_labels(TEX, {"Experimental setup": 1, "Conclusion": 1})
    blocks = body_blocks(TEX)
    final = []
    for b in blocks:
        if b["type"] == "h1" and b["text"] == "Experimental setup":
            final += implementation_blocks()
        if b["type"] == "h1" and b["text"] == "Conclusion":
            final += examples_blocks()
        final.append(b)
    title = re.search(r"\\title\{(.*?)\\ifcourse", TEX).group(1)
    submission_edits(final)
    doc = {"title": runs(title), "subtitle": "Detailed project report", "authors": "Sabuhi Aghayev and Ilkin Mammadli",
           "blocks": summary_blocks() + final}
    out = ROOT / "presentation/report/content.json"
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    left = sorted(set(re.findall(r"\\[A-Za-z]+", json.dumps(doc, ensure_ascii=False))))
    print(len(doc["blocks"]), "blocks; leftover LaTeX:", left[:40])
