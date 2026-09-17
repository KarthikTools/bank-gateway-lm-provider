pdf_unnest — nested tables out of PDFs, no LLM
Turns Word/Acrobat-style SOP PDFs (a Step | Action table whose cells hold If … | Then … tables, which hold further tables) into:

nested JSON that mirrors the real table hierarchy,
a readable HTML page for non-technical readers (nested tables rendered as nested tables, bold/italic/links kept),
an optional SOP-flavoured JSON (steps → actions → if/then → …).
Pure Python, one MIT-licensed dependency (pdfplumber), no model downloads, no API calls. A 4-page SOP takes about 0.3 s.

Quick start
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python unnest_pdf.py your_sop.pdf -o out.json --html out.html --sop sop.json
Options:

flag	meaning
--engine auto|tagged|geometry	auto (default) uses the PDF's structure tags when present, else geometry
--pages 8-9,12	subset of pages (handy while checking a big document)
--no-stitch	keep page fragments separate instead of merging rows/tables across page breaks
Sanity-check a document without any LLM: run both engines and diff the outlines. If they agree, you can trust the result; where they differ, look at that page.

.venv/bin/python unnest_pdf.py your_sop.pdf --engine tagged   -o a.json
.venv/bin/python unnest_pdf.py your_sop.pdf --engine geometry -o b.json
diff <(.venv/bin/python outline.py a.json) <(.venv/bin/python outline.py b.json)
Why this works (and why the usual tools don't)
1. Word-made PDFs are usually tagged. Word's Save as PDF and Acrobat PDFMaker write a logical structure tree (/StructTreeRoot) by default ("Document structure tags for accessibility"). It contains Table → TR → TD/TH → P | Table … — exactly the nesting the author built in Word. pdfplumber ≥ 0.10 exposes that tree and gives every character its marked-content id, so text can be routed to the right cell losslessly. The tagged engine walks that tree. Nesting, header cells, paragraph boundaries and reading order come straight from the file.

2. When tags are missing (print-to-PDF, re-distilled files, some document systems), the borders still are. Office producers draw each cell border as its own thin rectangle, and a nested table sits inside its cell inset by the cell margin (~5 pt sideways, 0.5 pt vertically). The geometry engine rebuilds cells from those segments, joining them only where they meet end to end (real corners and seams). Nested borders never meet the enclosing cell's borders end to end, so the enclosing cell survives, and a table whose box lies inside a cell is attached to that cell. Cell text is split into paragraphs by line spacing, bullets and indentation. pdfplumber's own finder remains the fallback for producers that draw long unbroken lines (verified with a reportlab sample).

3. Page breaks are stitched in both engines. Repeated header rows are dropped, a row that continues on the next page (only its right-hand cells are present) is merged into the previous row, nested tables that continue are merged recursively, and a sentence cut at the page break is re-joined when the continuation starts in lower case.

Off-the-shelf extractors (pdfplumber/PyMuPDF find_tables, Camelot, Tabula, pdf2docx, Docling) all flatten a nested table into one big grid with many empty cells, and none of them re-join tables across pages. That flattened grid is what made an LLM look necessary.

Output model
{ "source": "sop.pdf", "pages": 4, "engine": "tagged",
  "blocks": [                                   // document order
    { "type": "table", "page": 1, "rows": [
        { "header": true, "page": 1, "cells": [
            { "col": 0, "colspan": 1, "rowspan": 1, "header": true,
              "content": [ { "type": "paragraph", "page": 1, "text": "Step" } ] },
            { "col": 1, "colspan": 2, "rowspan": 1, "header": true,
              "content": [ { "type": "paragraph", "page": 1, "text": "Action" } ] } ] },
        { "header": false, "page": 1, "cells": [
            { "col": 0, "content": [ { "type": "paragraph", "text": "8" } ] },
            { "col": 1, "content": [ { "type": "paragraph", "text": "MANUAL and trace number starts with 9" } ] },
            { "col": 2, "content": [
                { "type": "paragraph", "text": "• Retrieve disposition in IRIS …" },
                { "type": "paragraph", "text": "Note: If any of …", "md": "**Note:** If any of …" },
                { "type": "table", "page": 1, "rows": [ … ] },      // nested table
                { "type": "paragraph", "text": "• Is transit group ALT BRANCH …" },
                { "type": "table", "page": 2, "rows": [ … ] } ] } ] } ] } ] }
text is always plain. md appears only when the paragraph has formatting: **bold**, *italic*, [link text](url).
A cell's content is an ordered list of paragraphs and tables, so text before, between and after nested tables keeps its place.
--sop writes the same data as {"steps": [{"step", "label", "actions": [text | {"if_then": [{"if", "then": [...]}]} | {"table": [...]}]}]}. That transform is deterministic because the SOP template is fixed; adapt sop_view() in unnest_pdf.py if your template differs.
Test results (all in samples/ and out/)
sample	how it was made	engine used	result
sop_sample_word.pdf (4 pages)	Word for Mac → Save as PDF, from make_sample_docx.py (3 levels of nesting, nested tables split over 3 page breaks, merged header, links, bold/italic)	tagged	1 outer table, 5 rows, 3 nested levels, all fragments re-joined, split sentence re-joined
same file, --engine geometry		geometry	identical outline to the tagged run
sop_sample_untagged.pdf	tags stripped with pikepdf	auto → geometry	identical outline
sop_sample_reportlab.pdf (2 pages)	reportlab (lines instead of rects, no tags), from make_sample_reportlab.py	auto → geometry (pdfplumber fallback)	3 levels, page-2 rows merged, repeated header dropped
The sample was modelled on the RBC "ADJ-SUSP-1 Account Set SOP" pages in the screenshot (same Step/Action layout, If/Then tables, Detail/Field table, continuation at the top of a page).

Market research: what else was considered
tool	licence	nested tables	cross-page	notes
pypdf	BSD	no (text only)	–	no layout information at all
pdfplumber (used here)	MIT	via structure tree + custom geometry (this tool)	this tool	exposes chars, rects, lines, annotations, structure tree, MCIDs
PyMuPDF find_tables	AGPL (commercial licence needed for proprietary use)	flattens	no	fast; its table finder is a port of pdfplumber's
Camelot (lattice)	MIT	flattens	no	OpenCV line detection; single-page grids
Tabula / tabula-py	MIT	flattens	no	Java
pdf2docx → python-docx	MIT, but depends on PyMuPDF (AGPL)	partly	no	tested on the sample: 4 separate tables (one per page), outer grid flattened to 6–9 columns, words split mid-word; README says it is no longer maintained by Artifex
Docling (IBM, TableFormer)	MIT	no	no	ML models, local; nested/merged tables are a documented limitation; useful for scanned PDFs
Marker / Unstructured	GPL / Apache	no	no	ML-based, flat table output
Adobe PDF Extract, Azure Document Intelligence, AWS Textract	paid per page	flat table objects (not verified for nesting)	no	cloud, cost, data leaves the bank
LLM (what you did)	$$$	yes	yes	slow, costly, non-deterministic
Sources: pdfplumber, pdf2docx, Docling discussion #2241, Docling issue #3158, PyMuPDF vs pdfplumber licence note, Camelot comparison.

Known limitations
Born-digital PDFs only. Scanned pages need OCR first (Docling or Tesseract), and OCR output has no reliable borders for the geometry engine.
Geometry engine: paragraph boundaries are inferred from spacing, so two paragraphs with zero space-after and no bullet may merge into one (happens in the reportlab sample, not in Word output). The tagged engine does not have this problem.
A row continuation at a page top is detected by "only the right-hand cells continue". A genuinely new row whose left-hand cells are all empty at a page top would be merged into the previous row. --no-stitch turns merging off.
Nested tables whose borders coincide exactly with the enclosing cell (cell margins set to 0) collapse into a flat grid, like every other tool.
Column spans in the tagged engine come from the drawn borders; if a page has no usable borders, spans fall back to 1.
Files
unnest_pdf.py — the tool (single file, ~1,100 lines, documented).
outline.py — prints a compact outline of a JSON output; use it to diff runs.
samples/make_sample_docx.py, samples/make_sample_reportlab.py — sample builders.
samples/*.pdf — test PDFs; samples/page1.png, page2.png — what they look like.
out/*.json, out/*.html — outputs of the runs above.
requirements.txt (runtime), requirements-dev.txt (sample builders).
