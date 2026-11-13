#!/usr/bin/env bash
# Build the paper: refreshes all results, then writes paper.pdf (ACL version) and course_paper.pdf (course version)
# to the repo root. Run from anywhere:   bash build_paper.sh
# Needs a LaTeX installation (TeX Live / MiKTeX: latexmk or pdflatex+bibtex; or tectonic). Without one it writes
# paper_overleaf.zip (upload to Overleaf, compile paper.tex or course_paper.tex).
set -u
cd "$(dirname "$0")"
ROOT="$(pwd)"
if [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe; elif [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python; fi

echo "== 1/3 regenerating tables, figures and RESULTS.md"
"$PY" make_results.py || echo "make_results.py failed: building with the existing tables"

cd paper/acl
echo "== 2/3 ACL style files"
if [ ! -f acl.sty ]; then
  if command -v git >/dev/null 2>&1; then
    rm -rf .acl_tmp
    if git clone --depth 1 -q https://github.com/acl-org/acl-style-files .acl_tmp; then
      for f in acl.sty acl_natbib.bst; do
        src=$(find .acl_tmp -name "$f" | head -1)
        [ -n "$src" ] && cp "$src" .
      done
    fi
    rm -rf .acl_tmp
  fi
  [ -f acl.sty ] && echo "fetched acl.sty" || echo "acl.sty not available: using the article fallback layout"
fi

echo "== 3/3 compiling"
build() {  # $1 = paper | course_paper
  rm -f "$1.aux" "$1.bbl" "$1.blg" "$1.fdb_latexmk" "$1.fls"
  if command -v pdflatex >/dev/null 2>&1; then
    pdflatex -interaction=nonstopmode "$1.tex" >"$1.build.log" 2>&1
    bibtex "$1" >>"$1.build.log" 2>&1
    pdflatex -interaction=nonstopmode "$1.tex" >>"$1.build.log" 2>&1
    pdflatex -interaction=nonstopmode "$1.tex" >>"$1.build.log" 2>&1
  elif command -v tectonic >/dev/null 2>&1; then
    tectonic "$1.tex" >"$1.build.log" 2>&1
  else
    return 2
  fi
  if [ -f "$1.pdf" ]; then cp "$1.pdf" "$ROOT/$1.pdf"; echo "wrote $1.pdf"; else echo "FAILED $1 (see paper/acl/$1.build.log)"; return 1; fi
}
status=0
for job in paper course_paper; do
  build "$job"; rc=$?
  if [ $rc -eq 2 ]; then status=2; break; fi
  [ $rc -ne 0 ] && status=1
done

if [ $status -eq 2 ]; then
  echo "no LaTeX found (latexmk, pdflatex or tectonic): writing paper_overleaf.zip instead"
  cd "$ROOT"
  "$PY" - <<'EOF'
import zipfile, pathlib
root = pathlib.Path(".")
with zipfile.ZipFile("paper_overleaf.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for f in pathlib.Path("paper/acl").iterdir():
        if f.suffix in (".tex", ".bib", ".sty", ".bst"):
            z.write(f, f.name)
    for d in ("tables", "figures"):
        for f in (root / d).glob("*"):
            if f.suffix in (".tex", ".pdf", ".png"):
                z.write(f, f"{d}/{f.name}")
print("wrote paper_overleaf.zip: upload to Overleaf, set the main document to paper.tex (or course_paper.tex)")
EOF
fi
exit $status
