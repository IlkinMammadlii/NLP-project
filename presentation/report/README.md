# Detailed report

`Detailed_Report.docx` / `Detailed_Report.pdf`: the paper (`paper/acl/main.tex`) expanded into a detailed report,
with two extra sections (Implementation, Qualitative examples). Every number comes from `tables/*.tex`,
`tables/macros.tex`, `cf_data/runs/results.jsonl`, `cf_data/cache/train/meta.json` and `presentation/examples.json`.

Rebuild (from the repo root):

1. `.venv\Scripts\python.exe presentation\report\build_content.py`: LaTeX + tables to `content.json`
2. `node presentation\report\build_docx.js` (needs the `docx` npm package): writes `Detailed_Report.build.docx`
3. In Word: `export_pdf.ps1` then `save_docx.ps1` (see the first lines of each). They refresh the table of contents.
   Hidden Word hung on this machine when writing into the project folder or when one script did both steps,
   so write to a temp folder and copy the files here.
