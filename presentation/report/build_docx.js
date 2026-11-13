// Build the detailed report (.docx) from presentation/report/content.json (made by build_content.py).
// Run from the repo root:  node presentation/report/build_docx.js   (needs the `docx` npm package on NODE_PATH)
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType, Table, TableRow, TableCell, WidthType,
  ShadingType, BorderStyle, ImageRun, ExternalHyperlink, TableOfContents, Footer, PageNumber, LevelFormat, PageBreak, TabStopType,
} = require("docx");

const ROOT = path.resolve(__dirname, "..", "..");
const C = JSON.parse(fs.readFileSync(path.join(__dirname, "content.json"), "utf8"));

const FONT = "Calibri", HEAD = "Cambria", MONO = "Consolas";
const COL = { teacher: "2563B8", student: "D9741C", tail: "C2257A", gold: "3F7D4E", ink: "1B2230", muted: "5D6676",
              headFill: "E8EDF5", groupFill: "F3F5F8", tailFill: "FBE6F1", line: "C9CFD9" };
const PAGE_W = 11906, MARGIN = 1418, WIDTH = PAGE_W - 2 * MARGIN; // A4, 2.5 cm margins

// ------------------------------------------------------------------ inline runs
function textRuns(runs, base = {}) {
  return runs.map(r => new TextRun({
    text: r.t, bold: r.b || base.bold, italics: r.i || base.italics, subScript: r.sub, superScript: r.sup,
    font: r.mono ? MONO : base.font, size: base.size, color: base.color,
  }));
}

// ------------------------------------------------------------------ numbering (bullets + one instance per ordered list)
const numbering = { config: [
  { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 540, hanging: 300 } } } }] },
  { reference: "ordered", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 540, hanging: 360 } } } }] },
] };
let listInstance = 0;

// ------------------------------------------------------------------ tables
function cell(runs, width, opts = {}) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    shading: opts.fill ? { type: ShadingType.CLEAR, color: "auto", fill: opts.fill } : undefined,
    margins: { top: 50, bottom: 50, left: 90, right: 90 },
    columnSpan: opts.span,
    children: [new Paragraph({ alignment: opts.align || AlignmentType.LEFT, keepNext: opts.keepNext,
      children: textRuns(runs, { size: opts.size, bold: opts.bold, italics: opts.italics }) })],
  });
}

function table(b, number) {
  const n = b.header.length;
  const first = n <= 2 ? Math.round(WIDTH * 0.32) : n <= 4 ? Math.round(WIDTH * 0.40) : n <= 6 ? Math.round(WIDTH * 0.30) : Math.round(WIDTH * 0.20);
  const rest = Math.floor((WIDTH - first) / (n - 1));
  const widths = [WIDTH - rest * (n - 1), ...Array(n - 1).fill(rest)];
  const size = n >= 8 ? 15 : n >= 6 ? 17 : 19; // half-points
  const right = i => (i === 0 || (n === 2) ? AlignmentType.LEFT : AlignmentType.CENTER);
  const rows = [new TableRow({ tableHeader: true, children: b.header.map((h, i) =>
    cell(h, widths[i], { fill: COL.headFill, size, bold: true, align: right(i) })) })];
  for (const r of b.rows) {
    if (r.group) {
      rows.push(new TableRow({ children: [cell(r.group, WIDTH, { span: n, fill: COL.groupFill, size, italics: true })] }));
    } else {
      const cells = r.cells.slice(0, n);
      while (cells.length < n) cells.push([]);
      rows.push(new TableRow({ cantSplit: true, children: cells.map((c, i) =>
        cell(c, widths[i], { size, align: right(i), fill: r.tail ? COL.tailFill : undefined })) }));
    }
  }
  const border = { style: BorderStyle.SINGLE, size: 4, color: COL.line };
  const cap = new Paragraph({ spacing: { before: 200, after: 80 }, keepNext: true, children: [
    ...(number ? [new TextRun({ text: `Table ${number}. `, bold: true, size: 19, font: FONT })] : []),
    ...textRuns(b.caption, { size: 19, color: COL.muted })] });
  return [cap, new Table({ width: { size: WIDTH, type: WidthType.DXA }, columnWidths: widths, rows,
    borders: { top: border, bottom: border, left: border, right: border, insideHorizontal: border, insideVertical: border } }),
    new Paragraph({ spacing: { after: 120 }, children: [] })];
}

// ------------------------------------------------------------------ figures
function pngSize(file) {
  const buf = fs.readFileSync(file);
  return { buf, w: buf.readUInt32BE(16), h: buf.readUInt32BE(20) };
}

function figure(b) {
  const { buf, w, h } = pngSize(path.join(ROOT, b.png));
  const maxW = 600; // px at 96 dpi = the text width
  const W = Math.min(maxW, b.wide ? maxW : 520), H = Math.round(W * h / w);
  return [
    new Paragraph({ alignment: AlignmentType.CENTER, keepNext: true, spacing: { before: 160 },
      children: [new ImageRun({ type: "png", data: buf, transformation: { width: W, height: H } })] }),
    new Paragraph({ spacing: { after: 200 }, children: [new TextRun({ text: `${b.label}. `, bold: true, size: 19 }),
      ...textRuns(b.caption, { size: 19, color: COL.muted })] }),
  ];
}

// ------------------------------------------------------------------ example boxes (qualitative section)
function example(b) {
  const color = { gold: COL.gold, teacher: COL.teacher, student: COL.student }[b.kind];
  const left = { left: { style: BorderStyle.SINGLE, size: 18, color, space: 8 } };
  const lines = b.text.split(/\n+/).filter(s => s.trim());
  return [
    new Paragraph({ keepNext: true, spacing: { before: 120, after: 20 }, indent: { left: 200 }, border: left,
      children: [new TextRun({ text: b.who, bold: true, color, size: 19 })] }),
    ...lines.map((l, i) => new Paragraph({ indent: { left: 200 }, border: left, spacing: { after: i === lines.length - 1 ? 80 : 20 },
      children: [new TextRun({ text: l, size: 19 })] })),
  ];
}

// ------------------------------------------------------------------ body
const body = [];
let h1 = 0, h2 = 0, appendix = -1, tableNo = 0, inAppendix = false;
for (const b of C.blocks) {
  switch (b.type) {
    case "appendix":
      inAppendix = true;
      break;
    case "h1": {
      let label = "";
      if (inAppendix && b.numbered) { appendix += 1; label = `Appendix ${"ABCDEFG"[appendix]}   `; }
      else if (b.numbered) { h1 += 1; label = `${h1}   `; }
      h2 = 0;
      body.push(new Paragraph({ heading: HeadingLevel.HEADING_1, pageBreakBefore: b.text === "References" || (inAppendix && appendix === 0 && b.numbered),
        children: [new TextRun(label + b.text)] }));
      break;
    }
    case "h2":
      h2 += 1;
      body.push(new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun(`${h1 ? h1 + "." + h2 + "   " : ""}${b.text}`)] }));
      break;
    case "para":
      body.push(new Paragraph({ spacing: { after: 140 }, alignment: AlignmentType.JUSTIFIED, children: textRuns(b.runs) }));
      break;
    case "list": {
      const inst = listInstance++;
      b.items.forEach(it => body.push(new Paragraph({ spacing: { after: 80 }, alignment: AlignmentType.JUSTIFIED,
        numbering: { reference: b.ordered ? "ordered" : "bullets", level: 0, instance: b.ordered ? inst : undefined },
        children: textRuns(it) })));
      body.push(new Paragraph({ spacing: { after: 60 }, children: [] }));
      break;
    }
    case "table":
      body.push(...table(b, b.label ? ++tableNo : null));
      break;
    case "figure":
      body.push(...figure(b));
      break;
    case "equation":
      body.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 120, after: 160 },
        tabStops: [{ type: TabStopType.RIGHT, position: WIDTH }],
        children: [...textRuns(b.runs, { font: "Cambria Math" }), new TextRun({ text: `\t(${b.label.split(" ")[1]})` })] }));
      break;
    case "example":
      body.push(...example(b));
      break;
    case "ref":
      body.push(new Paragraph({ indent: { left: 360, hanging: 360 }, spacing: { after: 100 }, children: textRuns(b.runs, { size: 19 }) }));
      break;
  }
}

// ------------------------------------------------------------------ title page + contents
const title = [
  new Paragraph({ spacing: { before: 2600, after: 300 }, children: textRuns(C.title, { font: HEAD, size: 44, bold: true, color: COL.ink }) }),
  new Paragraph({ border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: COL.student, space: 6 } }, spacing: { after: 400 },
    children: [new TextRun({ text: C.subtitle, font: HEAD, size: 30, color: COL.muted })] }),
  new Paragraph({ spacing: { after: 120 }, children: [new TextRun({ text: C.authors, size: 26, bold: true })] }),
  new Paragraph({ spacing: { after: 120 }, children: [new TextRun({ text: "November 2026", size: 24, color: COL.muted })] }),
  new Paragraph({ spacing: { after: 120 }, children: [new TextRun({ text: "Code: ", size: 22, bold: true }),
    new ExternalHyperlink({ link: "https://github.com/IlkinMammadlii/NLP-project", children: [new TextRun({ text: "https://github.com/IlkinMammadlii/NLP-project", size: 22, style: "Hyperlink", color: COL.teacher, underline: {} })] })] }),
  new Paragraph({ spacing: { before: 1600 }, children: [new TextRun({ text: "Teacher: SmolLM2-1.7B-Instruct   ·   Student: SmolLM2-135M   ·   Data: databricks-dolly-15k   ·   Hardware: one 8 GB laptop GPU", size: 20, color: COL.muted })] }),
  new Paragraph({ children: [new PageBreak()] }),
  new Paragraph({ spacing: { after: 200 }, children: [new TextRun({ text: "Contents", font: HEAD, size: 32, bold: true })] }),
  new TableOfContents("Contents", { hyperlink: true, headingStyleRange: "1-2" }),
  new Paragraph({ children: [new PageBreak()] }),
];

const doc = new Document({
  creator: C.authors, title: C.title.map(r => r.t).join(""), description: C.subtitle,
  numbering,
  styles: {
    default: { document: { run: { font: FONT, size: 22, color: COL.ink }, paragraph: { spacing: { line: 276 } } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: HEAD, size: 32, bold: true, color: COL.ink }, paragraph: { spacing: { before: 360, after: 160 }, keepNext: true, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: HEAD, size: 26, bold: true, color: COL.teacher }, paragraph: { spacing: { before: 260, after: 120 }, keepNext: true, outlineLevel: 1 } },
    ],
  },
  sections: [{
    properties: { page: { size: { width: PAGE_W, height: 16838 }, margin: { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN } } },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER,
      children: [new TextRun({ children: [PageNumber.CURRENT], size: 18, color: COL.muted })] })] }) },
    children: [...title, ...body],
  }],
});

const out = path.join(ROOT, "presentation", "report", "Detailed_Report.build.docx");
Packer.toBuffer(doc).then(buf => { fs.writeFileSync(out, buf); console.log("wrote", out, buf.length, "bytes"); });
