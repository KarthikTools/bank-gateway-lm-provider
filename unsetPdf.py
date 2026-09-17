#!/usr/bin/env python3
"""
unnest_pdf.py - recover NESTED tables from born-digital PDFs (Word / Acrobat
SOPs, procedures, policies) into a nested JSON model and a readable HTML page.
No LLM, no OCR, no cloud: pure geometry and PDF structure. MIT-licensed deps only.

Two engines, auto-selected per document:

  tagged    The PDF carries a logical structure tree (/Table /TR /TD ...).
            Word's "Save as PDF" and Acrobat PDFMaker write one by default.
            Nesting, header cells, paragraph boundaries and reading order are
            taken from the tags, so the result is essentially lossless.

  geometry  No tags (print-to-PDF, re-distilled, some DMS exports).
            Cells are rebuilt from the drawn borders (pdfplumber lines/rects),
            nesting is inferred from geometric containment (an inner table's box
            lies inside an outer cell's box), and cell text is split into
            paragraphs by line spacing / bullets / indentation.

Both engines emit the same model and share one post-processor that stitches
tables across page breaks (repeated header rows, rows and nested tables that
continue on the next page, paragraphs cut mid-sentence).

Usage:
    python unnest_pdf.py input.pdf                       # writes input.json
    python unnest_pdf.py input.pdf -o out.json --html out.html --sop sop.json
    python unnest_pdf.py input.pdf --engine geometry     # force an engine
    python unnest_pdf.py input.pdf --pages 8-9           # subset of pages

JSON model (all keys starting with "_" are internal and stripped on output):
    document  {source, pages, engine, blocks: [block]}
    block     paragraph {type:"paragraph", page, text, md?}     md = text with
                                                                 **bold**, *italic*, [link](url)
              table     {type:"table", page, rows:[row]}
    row       {header: bool, page, cells:[cell]}
    cell      {col, colspan, rowspan, header, content:[block]}   content may nest tables
"""
from __future__ import annotations

import argparse
import html as htmlmod
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from operator import itemgetter
from typing import Any, Dict, List, Optional, Tuple

import pdfplumber
from pdfplumber.structure import PDFStructTree, StructTreeMissing
from pdfplumber.utils import cluster_objects, extract_words

Block = Dict[str, Any]

# pdfplumber table-finder settings tuned for Word/Acrobat output: borders are
# thin filled rects (two edges ~0.5pt apart -> snap), nested tables are inset
# ~5pt from the enclosing cell (keep snap tolerance below that).
TABLE_SETTINGS = dict(
    vertical_strategy="lines",
    horizontal_strategy="lines",
    snap_tolerance=2,
    join_tolerance=2,
    intersection_tolerance=2,
    edge_min_length=3,
)

BULLET_RE = re.compile(r"^(?:[•▪◦●○■□\-–—*]|\(?\d{1,3}[.)]|[a-zA-Z][.)])(?:\s|$)")
PUA_BULLETS = {"": "•", "": "▪", "": "▪", "": "❖", "": "✓", "": "•"}
CID_RE = re.compile(r"\(cid:\d+\)")


# --------------------------------------------------------------------------- #
# Text assembly (shared by both engines)
# --------------------------------------------------------------------------- #
def _style(fontname: Optional[str]) -> Tuple[bool, bool]:
    fn = (fontname or "").lower()
    bold = any(k in fn for k in ("bold", "black", "heavy", "semibold", "demibold"))
    italic = "italic" in fn or "oblique" in fn
    return bold, italic


def _clean_word(text: str) -> str:
    for k, v in PUA_BULLETS.items():
        text = text.replace(k, v)
    if CID_RE.fullmatch(text):
        return "•"  # unmapped single glyph at word level: almost always a list bullet
    return CID_RE.sub("", text)


def chars_to_lines(chars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group chars into words, then words into lines (sorted top-to-bottom)."""
    if not chars:
        return []
    words = extract_words(
        chars,
        x_tolerance_ratio=0.25,
        y_tolerance=3,
        keep_blank_chars=False,
        extra_attrs=["fontname", "size"],
    )
    lines = []
    for cluster in cluster_objects(words, itemgetter("top"), tolerance=3):
        cluster = sorted(cluster, key=itemgetter("x0"))
        for w in cluster:
            w["text"] = _clean_word(w["text"])
        cluster = [w for w in cluster if w["text"].strip()]
        if not cluster:
            continue
        lines.append(
            {
                "words": cluster,
                "top": min(w["top"] for w in cluster),
                "bottom": max(w["bottom"] for w in cluster),
                "x0": min(w["x0"] for w in cluster),
                "x1": max(w["x1"] for w in cluster),
                "size": statistics.median(w.get("size", 9) for w in cluster),
                "text": " ".join(w["text"] for w in cluster),
            }
        )
    lines.sort(key=itemgetter("top"))
    return lines


def _link_for(word: Dict[str, Any], links: List[Dict[str, Any]]) -> Optional[str]:
    if not links:
        return None
    cx = (word["x0"] + word["x1"]) / 2
    cy = (word["top"] + word["bottom"]) / 2
    for ln in links:
        if ln["x0"] - 1 <= cx <= ln["x1"] + 1 and ln["top"] - 1 <= cy <= ln["bottom"] + 1:
            return ln.get("uri")
    return None


def lines_to_paragraph(lines: List[Dict[str, Any]], links: List[Dict[str, Any]], page: int) -> Optional[Block]:
    """Assemble one paragraph from consecutive lines. Produces plain `text`
    and, when bold/italic/links are present, an inline-markdown `md`."""
    tokens = []  # (text, bold, italic, href, separator-before)
    for line in lines:
        prev = None
        for w in line["words"]:
            bold, italic = _style(w.get("fontname"))
            href = _link_for(w, links)
            if prev is None:
                sep = " " if tokens else ""
            else:
                sep = " " if (w["x0"] - prev["x1"]) > 0.15 * max(w.get("size", 9), 1) else ""
            tokens.append((w["text"], bold, italic, href, sep))
            prev = w
    text = "".join(sep + t for t, _, _, _, sep in tokens).strip()
    if not text:
        return None
    runs: List[Dict[str, Any]] = []
    for t, b, i, h, sep in tokens:
        if runs and runs[-1]["b"] == b and runs[-1]["i"] == i and runs[-1]["h"] == h:
            runs[-1]["t"] += sep + t
        else:
            runs.append({"t": t, "b": b, "i": i, "h": h, "sep": sep})
    md = ""
    for r in runs:
        t = r["t"]
        if r["h"]:
            t = f"[{t}]({r['h']})"
        if r["b"] and r["i"]:
            t = f"***{t}***"
        elif r["b"]:
            t = f"**{t}**"
        elif r["i"]:
            t = f"*{t}*"
        md += r["sep"] + t
    md = md.strip()
    para: Block = {"type": "paragraph", "page": page, "text": text}
    if md != text:
        para["md"] = md
    para["_bbox"] = (
        min(l["x0"] for l in lines),
        min(l["top"] for l in lines),
        max(l["x1"] for l in lines),
        max(l["bottom"] for l in lines),
    )
    para["_bold"] = all(b for _, b, _, _, _ in tokens)
    return para


def split_paragraphs(lines: List[Dict[str, Any]], page_pitch: float) -> List[List[Dict[str, Any]]]:
    """Split a vertical run of lines into paragraphs using vertical gaps,
    list bullets and dedents (geometry engine only; the tagged engine gets
    paragraph boundaries from the PDF)."""
    paras: List[List[Dict[str, Any]]] = []
    if not lines:
        return paras
    right_edge = max(l["x1"] for l in lines)
    cur: List[Dict[str, Any]] = []
    for ln in lines:
        if cur:
            prev = cur[-1]
            size = max(prev["size"], ln["size"], 1)
            gap = ln["top"] - prev["top"]
            threshold = max(1.6 * size, 1.3 * page_pitch) if page_pitch else 1.6 * size
            new = gap > threshold
            new = new or bool(BULLET_RE.match(ln["text"]))
            # dedent relative to the paragraph's first line, when the previous
            # line ended short (so first-line-indent prose is not split)
            if not new and ln["x0"] < cur[0]["x0"] - 4 and prev["x1"] < right_edge - 12:
                new = True
            if new:
                paras.append(cur)
                cur = []
        cur.append(ln)
    if cur:
        paras.append(cur)
    return paras


def page_line_pitch(lines: List[Dict[str, Any]]) -> float:
    diffs = []
    for a, b in zip(lines, lines[1:]):
        d = b["top"] - a["top"]
        if 0 < d < 3 * max(a["size"], 1):
            diffs.append(d)
    return statistics.median(diffs) if diffs else 0.0


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def _cluster_values(values: List[float], tol: float = 2.0) -> List[float]:
    values = sorted(values)
    out: List[List[float]] = []
    for v in values:
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [statistics.mean(g) for g in out]


def _index_of(boundaries: List[float], v: float) -> int:
    return min(range(len(boundaries)), key=lambda i: abs(boundaries[i] - v))


def _contains(outer, inner, tol: float = 2.0) -> bool:
    return (
        outer[0] - tol <= inner[0]
        and outer[1] - tol <= inner[1]
        and outer[2] + tol >= inner[2]
        and outer[3] + tol >= inner[3]
    )


def _area(b) -> float:
    return max(b[2] - b[0], 0) * max(b[3] - b[1], 0)


def _center_in(b, obj) -> bool:
    cx = (obj["x0"] + obj["x1"]) / 2
    cy = (obj["top"] + obj["bottom"]) / 2
    return b[0] <= cx <= b[2] and b[1] <= cy <= b[3]


def _union(bboxes):
    bboxes = [b for b in bboxes if b]
    if not bboxes:
        return None
    return (
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    )


def _touch(a, b, tol: float = 2.0) -> bool:
    return (min(a[2], b[2]) - max(a[0], b[0]) >= -tol) and (min(a[3], b[3]) - max(a[1], b[1]) >= -tol)


def _connected_groups(rects: List[tuple]) -> List[List[tuple]]:
    """Group rectangles that touch (share an edge or a corner) into tables."""
    parent = list(range(len(rects)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            if _touch(rects[i], rects[j]):
                parent[find(i)] = find(j)
    groups: Dict[int, List[tuple]] = defaultdict(list)
    for i, r in enumerate(rects):
        groups[find(i)].append(r)
    return [sorted(g, key=lambda r: (r[1], r[0])) for g in groups.values()]


# ---- border-segment cell finder ------------------------------------------- #
# Word (and most office producers) draw every table-cell border as its own thin
# rect. Nested tables are drawn ~0.5pt inside the enclosing cell, and their
# segments never meet the enclosing cell's segments end-to-end. pdfplumber's
# finder snaps everything within 2-3pt into one grid, so a nested table that
# fills a cell top-to-bottom hides the enclosing cell behind two 5pt "strip"
# cells. We therefore rebuild cells from the raw segments, joining them only at
# real corners/seams (end-to-end contacts), and fall back to pdfplumber when a
# producer draws long unbroken lines instead (no end-to-end contacts).
THIN = 3.0        # max thickness of a border rect
MIN_LEN = 3.0     # ignore shorter segments
COLLINEAR = 0.6   # max offset between collinear segments of one chain
SEAM_GAP = 1.2    # max gap between end-to-end segments of one chain
NODE_TOL = 1.2    # max distance for a corner / seam contact


def _segments(page):
    H, V = [], []  # (coord, start, end): coord = y for horizontals, x for verticals
    for r in list(page.rects) + list(page.lines):
        w, h = r["x1"] - r["x0"], r["bottom"] - r["top"]
        if h <= THIN and w >= MIN_LEN:
            H.append(((r["top"] + r["bottom"]) / 2, r["x0"], r["x1"]))
        elif w <= THIN and h >= MIN_LEN:
            V.append(((r["x0"] + r["x1"]) / 2, r["top"], r["bottom"]))
    return H, V


def _chains(segs):
    """Join collinear segments that meet end-to-end. Returns a list of chains:
    {coord, start, end, breaks} where breaks = start, end and every seam."""
    segs = sorted(segs, key=lambda s: (s[1], s[0]))
    n = len(segs)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        ci, ai, bi = segs[i]
        for j in range(i + 1, n):
            cj, aj, bj = segs[j]
            if aj - bi > SEAM_GAP:
                break  # sorted by start: no later segment can abut segment i
            if abs(ci - cj) <= COLLINEAR and -COLLINEAR <= aj - bi <= SEAM_GAP:
                parent[find(i)] = find(j)
    groups: Dict[int, list] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(segs[i])
    chains = []
    for g in groups.values():
        g.sort(key=lambda s: s[1])
        breaks = [g[0][1]] + [(a[2] + b[1]) / 2 for a, b in zip(g, g[1:])] + [g[-1][2]]
        chains.append({"coord": statistics.mean(s[0] for s in g), "start": g[0][1], "end": g[-1][2], "breaks": breaks})
    return chains


def _near(values, v, tol=NODE_TOL):
    return any(abs(x - v) <= tol for x in values)


def segment_cells(page) -> List[Tuple[float, float, float, float]]:
    """Cells = smallest rectangles whose corners are nodes; a node is where a
    horizontal chain and a vertical chain meet at a break of both."""
    H, V = _segments(page)
    hc, vc = _chains(H), _chains(V)
    nodes: Dict[Tuple[int, int], Tuple[float, float]] = {}
    on_h: Dict[int, list] = defaultdict(list)
    on_v: Dict[int, list] = defaultdict(list)
    for hi, h in enumerate(hc):
        for vi, v in enumerate(vc):
            if h["start"] - NODE_TOL <= v["coord"] <= h["end"] + NODE_TOL and v["start"] - NODE_TOL <= h["coord"] <= v["end"] + NODE_TOL:
                if _near(h["breaks"], v["coord"]) and _near(v["breaks"], h["coord"]):
                    nodes[(hi, vi)] = (v["coord"], h["coord"])
                    on_h[hi].append(vi)
                    on_v[vi].append(hi)
    for hi in on_h:
        on_h[hi].sort(key=lambda vi: vc[vi]["coord"])
    for vi in on_v:
        on_v[vi].sort(key=lambda hi: hc[hi]["coord"])
    cells = []
    for (hi, vi), (x, y) in nodes.items():
        rights = [r for r in on_h[hi] if vc[r]["coord"] > x + 1]
        belows = [b for b in on_v[vi] if hc[b]["coord"] > y + 1]
        found = None
        for b in belows:
            for r in rights:
                if (b, r) in nodes:
                    found = (x, y, vc[r]["coord"], hc[b]["coord"])
                    break
            if found:
                break
        if found:
            cells.append(found)
    return cells


def geometric_cells(page) -> List[Tuple[float, float, float, float]]:
    """All border-delimited cells on a page: segment-exact finder, with
    pdfplumber's line finder as fallback for producers drawing long lines."""
    cells = segment_cells(page)
    try:
        fallback = [tuple(c) for t in page.find_tables(TABLE_SETTINGS) for c in t.cells]
    except Exception:  # pragma: no cover
        fallback = []
    if len(cells) < 0.5 * len(fallback):
        cells = fallback
    return list(dict.fromkeys(tuple(round(v, 2) for v in c) for c in cells))


# --------------------------------------------------------------------------- #
# Engine 1: tagged PDF (structure tree)
# --------------------------------------------------------------------------- #
class TaggedEngine:
    name = "tagged"

    def __init__(self, pdf: pdfplumber.PDF, pages: Optional[List[int]] = None):
        self.pdf = pdf
        self.tree = PDFStructTree(pdf)  # raises StructTreeMissing when untagged
        self.pages = set(pages) if pages else None
        self._page_cache: Dict[int, Dict[str, Any]] = {}

    # -- per-page indexes --------------------------------------------------- #
    def _pd(self, pn: int) -> Dict[str, Any]:
        if pn not in self._page_cache:
            page = self.pdf.pages[pn - 1]
            by: Dict[int, list] = defaultdict(list)
            first: Dict[int, int] = {}
            for idx, c in enumerate(page.chars):
                m = c.get("mcid")
                if m is None:
                    continue
                by[m].append(c)
                first.setdefault(m, idx)
            self._page_cache[pn] = {
                "by": by,
                "first": first,
                "links": page.hyperlinks,
                "cells": geometric_cells(page),
            }
        return self._page_cache[pn]

    def _element_chars(self, el) -> List[Tuple[int, list]]:
        keys = {(pn, m) for pn, m in el.all_mcids() if pn is not None}
        if self.pages:
            keys = {k for k in keys if k[0] in self.pages}
        ordered = sorted(keys, key=lambda k: (k[0], self._pd(k[0])["first"].get(k[1], 1 << 30)))
        out: List[Tuple[int, list]] = []
        for pn, m in ordered:
            chars = self._pd(pn)["by"].get(m, [])
            if out and out[-1][0] == pn:
                out[-1][1].extend(chars)
            else:
                out.append((pn, list(chars)))
        return out

    # -- flow content ------------------------------------------------------- #
    def _paragraphs(self, el) -> List[Block]:
        paras = []
        for pn, chars in self._element_chars(el):
            lines = chars_to_lines(chars)
            p = lines_to_paragraph(lines, self._pd(pn)["links"], pn) if lines else None
            if p:
                paras.append(p)
        return paras

    def _flow(self, el) -> List[Block]:
        if el.type == "Table":
            t = self._table(el)
            return [t] if t else []
        if el.find("Table") is None:
            return self._paragraphs(el)
        blocks: List[Block] = []
        if el.mcids:  # rare: text directly on a container that also holds a table
            own = type(el)(**{**el.__dict__, "children": []})
            blocks += self._paragraphs(own)
        for ch in el.children:
            blocks += self._flow(ch)
        blocks.sort(key=lambda b: (b.get("page", 0), (b.get("_bbox") or (0, 0))[1]))
        return blocks

    # -- tables ------------------------------------------------------------- #
    def _cell(self, el) -> Block:
        content = self._flow_children(el)
        page, bbox = None, None
        for pn, chars in self._element_chars(el):  # every char in the cell, nested tables included
            if chars:
                page = pn
                bbox = (min(c["x0"] for c in chars), min(c["top"] for c in chars),
                        max(c["x1"] for c in chars), max(c["bottom"] for c in chars))
                break
        # box used to find the enclosing border cell: it must also enclose the
        # border boxes of nested tables (else the smallest match is a nested cell)
        match = [bbox] + [
            (b["_gext"][0] - 1, b["_gext"][1] - 1, b["_gext"][2] + 1, b["_gext"][3] + 1)
            for b in content if b["type"] == "table" and b.get("_gext")
        ]
        return {
            "col": 0,
            "colspan": 1,
            "rowspan": 1,
            "header": el.type == "TH",
            "content": content,
            "_bbox": bbox,
            "_match": _union(match),
            "_page": page,
        }

    def _flow_children(self, el) -> List[Block]:
        blocks: List[Block] = []
        if el.mcids:
            own = type(el)(**{**el.__dict__, "children": []})
            blocks += self._paragraphs(own)
        for ch in el.children:
            blocks += self._flow(ch)
        blocks.sort(key=lambda b: (b.get("page", 0), (b.get("_bbox") or (0, 0))[1]))
        return blocks

    def _row(self, tr, header: bool) -> Optional[Block]:
        cells = [self._cell(c) for c in tr.children if c.type in ("TD", "TH")]
        cells = [c for c in cells if c["_page"] is not None]
        if not cells:
            return None
        page = min(c["_page"] for c in cells)
        return {"header": header or all(c["header"] for c in cells), "page": page, "cells": cells}

    def _table(self, el) -> Optional[Block]:
        rows: List[Block] = []
        for ch in el.children:
            if ch.type == "TR":
                r = self._row(ch, False)
                if r:
                    rows.append(r)
            elif ch.type in ("THead", "TBody", "TFoot"):
                for tr in ch.children:
                    if tr.type == "TR":
                        r = self._row(tr, ch.type == "THead")
                        if r:
                            rows.append(r)
        if not rows:
            return None
        table: Block = {
            "type": "table",
            "page": rows[0]["page"],
            "rows": rows,
            "_engine": "tagged",
            "_bbox": _union([c["_bbox"] for r in rows for c in r["cells"]]),
        }
        self._assign_columns(table)
        return table

    def _geom_rect(self, cell: Block):
        """Smallest border-delimited cell that encloses the tagged cell's text."""
        if cell["_match"] is None or cell["_page"] is None:
            return None
        best = None
        for rect in self._pd(cell["_page"])["cells"]:
            if _contains(rect, cell["_match"], tol=2.5) and (best is None or _area(rect) < _area(best)):
                best = rect
        return best

    def _assign_columns(self, table: Block) -> None:
        rows = table["rows"]
        for r in rows:
            for c in r["cells"]:
                rect = self._geom_rect(c)
                if rect:
                    c["_x0"], c["_x1"], c["_rect"], c["_exact"] = rect[0], rect[2], rect, True
                elif c["_bbox"]:
                    c["_x0"], c["_x1"], c["_rect"], c["_exact"] = c["_bbox"][0], c["_bbox"][2], None, False
                else:
                    c["_x0"], c["_x1"], c["_rect"], c["_exact"] = 0.0, 0.0, None, False
        exact = [c for r in rows for c in r["cells"] if c["_exact"]]
        if exact and len(exact) == sum(len(r["cells"]) for r in rows):
            xs = _cluster_values([c["_x0"] for c in exact] + [c["_x1"] for c in exact], tol=2.5)
            table["_xs"] = xs
            table["_gext"] = (xs[0], min(c["_rect"][1] for c in exact), xs[-1], max(c["_rect"][3] for c in exact))
            for r in rows:
                for c in r["cells"]:
                    i, j = _index_of(xs, c["_x0"]), _index_of(xs, c["_x1"])
                    c["col"], c["colspan"] = i, max(j - i, 1)
                r["cells"].sort(key=itemgetter("col"))
            # rowspan: a cell whose border box covers the following rows' boxes
            for ri, r in enumerate(rows):
                for c in r["cells"]:
                    span = 1
                    for nr in rows[ri + 1:]:
                        nb = _union([x["_bbox"] for x in nr["cells"]])
                        if nb and nr["page"] == r["page"] and c["_rect"][3] >= nb[3] - 2 and c["_rect"][1] <= nb[1] + 2:
                            span += 1
                        else:
                            break
                    c["rowspan"] = span
        else:
            # fallback: columns from text boxes of the widest rows
            ncols = max(len(r["cells"]) for r in rows)
            ranges = [[1e9, -1e9] for _ in range(ncols)]
            for r in rows:
                if len(r["cells"]) == ncols:
                    for i, c in enumerate(r["cells"]):
                        ranges[i][0] = min(ranges[i][0], c["_x0"])
                        ranges[i][1] = max(ranges[i][1], c["_x1"])
            for r in rows:
                used = set()
                for c in r["cells"]:
                    col = _best_overlap(ranges, c["_x0"], c["_x1"])
                    while col in used and col < ncols - 1:
                        col += 1
                    used.add(col)
                    c["col"], c["colspan"] = col, 1
                r["cells"].sort(key=itemgetter("col"))
            table["_xs"] = [ranges[0][0]] + [rg[1] for rg in ranges]
            table["_gext"] = table["_bbox"]

    # -- document ----------------------------------------------------------- #
    def blocks(self) -> List[Block]:
        out: List[Block] = []
        for el in self.tree.children:
            out += self._flow(el)
        return out


def _best_overlap(ranges, x0, x1) -> int:
    best, best_ov = 0, -1e9
    for i, (a, b) in enumerate(ranges):
        ov = min(b, x1) - max(a, x0)
        if ov <= 0:
            ov = -min(abs(a - x0), abs(b - x1))  # negative distance when disjoint
        if ov > best_ov:
            best, best_ov = i, ov
    return best


# --------------------------------------------------------------------------- #
# Engine 2: geometry (untagged PDFs)
# --------------------------------------------------------------------------- #
class GeometryEngine:
    name = "geometry"

    def __init__(self, pdf: pdfplumber.PDF, pages: Optional[List[int]] = None):
        self.pdf = pdf
        self.page_numbers = pages or [p.page_number for p in pdf.pages]
        self.hf = self._repeated_header_footer()

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text)).strip().lower()

    def _repeated_header_footer(self):
        counts: Counter = Counter()
        for pn in self.page_numbers:
            page = self.pdf.pages[pn - 1]
            h = page.height
            seen = set()
            for ln in chars_to_lines(page.chars):
                if ln["top"] < 0.12 * h or ln["bottom"] > 0.88 * h:
                    seen.add(self._norm(ln["text"]))
            counts.update(seen)
        need = max(2, int(0.5 * len(self.page_numbers) + 0.5))
        return {t for t, n in counts.items() if n >= need}

    def _page_tables(self, page) -> List[Block]:
        return self._build(geometric_cells(page), page.page_number)

    def _build(self, cells, pn: int) -> List[Block]:
        """Turn a set of border cells into top-level tables; cells lying strictly
        inside another cell form nested tables (recursively). This works whether
        the nested table is inset from the enclosing cell or touches its border."""
        parent: Dict[tuple, Optional[tuple]] = {}
        for c in cells:
            cands = [o for o in cells if o != c and _area(o) > _area(c) + 1 and _contains(o, c, tol=1.5)]
            parent[c] = min(cands, key=_area) if cands else None
        roots = [c for c in cells if parent[c] is None]
        tables: List[Block] = []
        for group in _connected_groups(roots):
            t = self._make_table(group, pn)
            for r in t["rows"]:
                for cell in r["cells"]:
                    inner = [c for c in cells if parent[c] is not None and c != cell["_rect"] and _contains(cell["_rect"], c, tol=1.5)]
                    if inner:
                        cell["_children"] = self._build(inner, pn)
            tables.append(t)
        return tables

    def _make_table(self, cells, pn: int) -> Block:
        xs = _cluster_values([c[0] for c in cells] + [c[2] for c in cells], 2.5)
        ys = _cluster_values([c[1] for c in cells] + [c[3] for c in cells], 2.5)
        rows = [{"header": False, "page": pn, "cells": []} for _ in range(max(len(ys) - 1, 1))]
        for c in cells:
            ci, cj = _index_of(xs, c[0]), _index_of(xs, c[2])
            ri, rj = _index_of(ys, c[1]), _index_of(ys, c[3])
            rows[ri]["cells"].append(
                {
                    "col": ci,
                    "colspan": max(cj - ci, 1),
                    "rowspan": max(rj - ri, 1),
                    "header": False,
                    "content": [],
                    "_rect": c,
                    "_x0": xs[ci],
                    "_x1": xs[cj],
                    "_exact": True,
                    "_page": pn,
                    "_bbox": c,
                }
            )
        rows = [r for r in rows if r["cells"]]
        for r in rows:
            r["cells"].sort(key=itemgetter("col"))
        bbox = _union(cells)
        return {
            "type": "table",
            "page": pn,
            "rows": rows,
            "_xs": xs,
            "_bbox": bbox,
            "_gext": bbox,
            "_engine": "geometry",
        }

    def _fill_table(self, table: Block, chars: list, links, pitch: float) -> None:
        for r in table["rows"]:
            for c in r["cells"]:
                cell_chars = [ch for ch in chars if _center_in(c["_rect"], ch)]
                own = cell_chars
                children = c.get("_children", [])
                for child in children:
                    sub = [ch for ch in own if _center_in(child["_bbox"], ch)]
                    own = [ch for ch in own if not _center_in(child["_bbox"], ch)]
                    self._fill_table(child, sub, links, pitch)
                blocks: List[Block] = []
                for para_lines in split_paragraphs(chars_to_lines(own), pitch):
                    p = lines_to_paragraph(para_lines, links, table["page"])
                    if p:
                        blocks.append(p)
                blocks += children
                blocks.sort(key=lambda b: (b.get("_bbox") or (0, 0))[1])
                c["content"] = blocks
                c.pop("_children", None)
        first = table["rows"][0]
        paras = [b for c in first["cells"] for b in c["content"] if b["type"] == "paragraph"]
        if paras and all(p.get("_bold") for p in paras) and len(table["rows"]) > 1:
            first["header"] = True
            for c in first["cells"]:
                c["header"] = True

    def page_blocks(self, page) -> List[Block]:
        pn = page.page_number
        links = page.hyperlinks
        all_lines = chars_to_lines(page.chars)
        pitch = page_line_pitch(all_lines)
        top = self._page_tables(page)
        chars = list(page.chars)
        for t in top:
            inside = [c for c in chars if _center_in(t["_bbox"], c)]
            chars = [c for c in chars if not _center_in(t["_bbox"], c)]
            self._fill_table(t, inside, links, pitch)
        blocks: List[Block] = list(top)
        lines = [ln for ln in chars_to_lines(chars) if self._norm(ln["text"]) not in self.hf]
        for para_lines in split_paragraphs(lines, pitch):
            p = lines_to_paragraph(para_lines, links, pn)
            if p:
                blocks.append(p)
        blocks.sort(key=lambda b: (b.get("_bbox") or (0, 0))[1])
        return blocks

    def blocks(self) -> List[Block]:
        out: List[Block] = []
        for pn in self.page_numbers:
            out += self.page_blocks(self.pdf.pages[pn - 1])
        return out


# --------------------------------------------------------------------------- #
# Shared post-processing: stitch across page breaks
# --------------------------------------------------------------------------- #
def _cell_text(cell: Block) -> str:
    return "\n".join(_block_text(b) for b in cell["content"]).strip()


def _block_text(b: Block) -> str:
    if b["type"] == "paragraph":
        return b["text"]
    return "\n".join(_cell_text(c) for r in b["rows"] for c in r["cells"])


def _row_sig(row: Block) -> str:
    return "|".join(re.sub(r"\s+", " ", _cell_text(c)).strip().lower() for c in row["cells"])


def _last_page(block: Block) -> int:
    if block["type"] == "paragraph":
        return block["page"]
    pages = [_last_page(b) for r in block["rows"] for c in r["cells"] for b in c["content"]]
    return max([block["page"]] + [r["page"] for r in block["rows"]] + pages)


def _ncols(table: Block) -> int:
    return max(1, len(table["_xs"]) - 1) if table.get("_xs") else max(len(r["cells"]) for r in table["rows"])


def _is_fragment(prev: Block, row: Block, table: Block) -> bool:
    """Is `row` the continuation (after a page break) of `prev`?"""
    if row["page"] <= prev["page"] or row.get("header"):
        return False
    nonempty = [c["col"] for c in row["cells"] if c["content"]]
    if not nonempty:
        return False
    first = min(nonempty)
    if first == 0:
        return False
    if table.get("_engine") == "tagged":
        # Word tags every real cell (empty ones carry a blank paragraph); a
        # continuation fragment simply lacks the cells that ended on the page before
        return min(c["col"] for c in row["cells"]) >= 1
    # geometry: cells always exist; treat "only the last column has text" as continuation
    ncols = _ncols(table)
    return first >= ncols - 1 and all(not c["content"] for c in row["cells"] if c["col"] < first)


def _looks_split(prev_text: str, next_text: str) -> bool:
    if not prev_text or not next_text:
        return False
    return next_text[0].islower() or prev_text.endswith(("-", "–"))


def _merge_content(a: List[Block], b: List[Block]) -> None:
    if a and b:
        if a[-1]["type"] == "table" and b[0]["type"] == "table" and _tables_compatible(a[-1], b[0]):
            merge_tables(a[-1], b[0])
            b = b[1:]
        elif a[-1]["type"] == "paragraph" and b[0]["type"] == "paragraph" and _looks_split(a[-1]["text"], b[0]["text"]):
            a[-1]["text"] = a[-1]["text"] + " " + b[0]["text"]
            if "md" in a[-1] or "md" in b[0]:
                a[-1]["md"] = a[-1].get("md", a[-1]["text"]) + " " + b[0].get("md", b[0]["text"])
            b = b[1:]
    a.extend(b)


def _merge_rows(prev: Block, row: Block) -> None:
    by_col = {c["col"]: c for c in prev["cells"]}
    for c in row["cells"]:
        target = by_col.get(c["col"])
        if target is None:
            prev["cells"].append(c)
            by_col[c["col"]] = c
        else:
            _merge_content(target["content"], c["content"])
    prev["cells"].sort(key=itemgetter("col"))


def _tables_compatible(a: Block, b: Block, tol: float = 4.0) -> bool:
    if b["page"] <= _last_page(a):
        return False
    xs = a.get("_xs")
    if not xs:
        return False
    for r in b["rows"]:
        for c in r["cells"]:
            if c.get("_exact"):
                if min(abs(x - c["_x0"]) for x in xs) > tol or min(abs(x - c["_x1"]) for x in xs) > tol:
                    return False
            else:
                if min(abs(x - c["_x0"]) for x in xs) > 3 * tol:
                    return False
    return True


def _reassign_columns(table: Block, row: Block) -> None:
    xs = table["_xs"]
    ranges = list(zip(xs, xs[1:]))
    for c in row["cells"]:
        if c.get("_exact"):
            i, j = _index_of(xs, c["_x0"]), _index_of(xs, c["_x1"])
            c["col"], c["colspan"] = i, max(j - i, 1)
        else:
            c["col"], c["colspan"] = _best_overlap(ranges, c["_x0"], c["_x1"]), 1
    row["cells"].sort(key=itemgetter("col"))


def merge_tables(a: Block, b: Block) -> None:
    """Append table b (next page) onto table a, then normalise."""
    for r in b["rows"]:
        _reassign_columns(a, r)
    rows = b["rows"]
    if rows and a["rows"] and _row_sig(rows[0]) == _row_sig(a["rows"][0]):
        a["rows"][0]["header"] = True  # repeated header row
        rows = rows[1:]
    a["rows"].extend(rows)
    normalize_table(a)


def normalize_table(table: Block) -> None:
    out: List[Block] = []
    header_sigs = set()
    for r in table["rows"]:
        if r.get("header"):
            sig = _row_sig(r)
            if sig in header_sigs:
                continue
            header_sigs.add(sig)
        if out and _is_fragment(out[-1], r, table):
            _merge_rows(out[-1], r)
            continue
        out.append(r)
    table["rows"] = out
    for r in out:
        for c in r["cells"]:
            for b in c["content"]:
                if b["type"] == "table":
                    normalize_table(b)


def stitch_blocks(blocks: List[Block]) -> List[Block]:
    out: List[Block] = []
    for b in blocks:
        if out and out[-1]["type"] == "table" and b["type"] == "table" and b["page"] == _last_page(out[-1]) + 1 and _tables_compatible(out[-1], b):
            merge_tables(out[-1], b)
            continue
        out.append(b)
    for b in out:
        if b["type"] == "table":
            normalize_table(b)
    return out


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def strip_internal(obj):
    if isinstance(obj, dict):
        return {k: strip_internal(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [strip_internal(v) for v in obj]
    return obj


_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _inline_html(p: Block) -> str:
    if "md" not in p:
        return htmlmod.escape(p["text"])
    s = htmlmod.escape(p["md"])
    s = _MD_LINK.sub(r'<a href="\2">\1</a>', s)
    s = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)
    return s


def _block_html(b: Block, depth: int) -> str:
    if b["type"] == "paragraph":
        return f"<p>{_inline_html(b)}</p>"
    rows = []
    for r in b["rows"]:
        cells = []
        for c in r["cells"]:
            tag = "th" if (c.get("header") or r.get("header")) else "td"
            attrs = ""
            if c.get("colspan", 1) > 1:
                attrs += f' colspan="{c["colspan"]}"'
            if c.get("rowspan", 1) > 1:
                attrs += f' rowspan="{c["rowspan"]}"'
            inner = "".join(_block_html(x, depth + 1) for x in c["content"])
            cells.append(f"<{tag}{attrs}>{inner}</{tag}>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f'<table class="lvl{min(depth, 3)}" data-page="{b["page"]}">' + "".join(rows) + "</table>"


HTML_CSS = """
body{font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.35;margin:24px;color:#222;max-width:1100px}
table{border-collapse:collapse;width:100%;margin:6px 0 10px}
td,th{border:1px solid #9a9a9a;vertical-align:top;padding:5px 7px;text-align:left}
th{background:#e9eef6;font-weight:bold}
table.lvl1 th{background:#f1f4f9} table.lvl2 th{background:#f7f8fb}
table.lvl1{border-left:3px solid #7a93c4} table.lvl2{border-left:3px solid #b3c2e0}
p{margin:0 0 6px} td>p:last-child,th>p:last-child{margin-bottom:0}
a{color:#0b5cad}
.meta{color:#666;font-size:12px;margin-bottom:14px}
"""


def render_html(doc: Dict[str, Any]) -> str:
    body = "".join(_block_html(b, 0) for b in doc["blocks"])
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{htmlmod.escape(doc['source'])}</title><style>{HTML_CSS}</style></head><body>"
        f"<div class='meta'>{htmlmod.escape(doc['source'])} · {doc['pages']} pages · engine: {doc['engine']}</div>"
        f"{body}</body></html>"
    )


# --------------------------------------------------------------------------- #
# Optional: SOP-flavoured view (Step/Action + If/Then tables -> steps & conditions)
# --------------------------------------------------------------------------- #
def _header_texts(table: Block) -> List[str]:
    first = table["rows"][0]
    return [re.sub(r"\s+", " ", _cell_text(c)).strip().lower() for c in first["cells"]]


def _items(blocks: List[Block]) -> List[Any]:
    items: List[Any] = []
    for b in blocks:
        if b["type"] == "paragraph":
            items.append(b["text"])
            continue
        hdr = _header_texts(b)
        rows = b["rows"]
        if len(hdr) >= 2 and hdr[0].startswith("if") and hdr[1].startswith("then"):
            items.append(
                {"if_then": [{"if": _cell_text(r["cells"][0]), "then": _items(r["cells"][-1]["content"])} for r in rows[1:] if r["cells"]]}
            )
        else:
            items.append({"table": [[_items(c["content"]) if any(x["type"] == "table" for x in c["content"]) else _cell_text(c) for c in r["cells"]] for r in rows]})
    return items


def sop_view(doc: Dict[str, Any]) -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = []
    other: List[Any] = []
    for b in doc["blocks"]:
        if b["type"] == "table" and _header_texts(b) and _header_texts(b)[0].startswith("step"):
            for r in b["rows"][1:]:
                cells = r["cells"]
                if not cells:
                    continue
                step = {"step": _cell_text(cells[0]) if len(cells) > 1 else ""}
                if len(cells) > 2:
                    step["label"] = _cell_text(cells[1])
                step["actions"] = _items(cells[-1]["content"])
                step["page"] = r["page"]
                steps.append(step)
        else:
            other += _items([b])
    return {"source": doc["source"], "engine": doc["engine"], "steps": steps, "other": other}


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def parse_pages(spec: Optional[str], n: int) -> Optional[List[int]]:
    if not spec:
        return None
    pages: List[int] = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            pages += list(range(int(a), int(b) + 1))
        else:
            pages.append(int(part))
    return [p for p in pages if 1 <= p <= n]


def extract(pdf_path: str, engine: str = "auto", pages: Optional[str] = None, stitch: bool = True) -> Dict[str, Any]:
    with pdfplumber.open(pdf_path) as pdf:
        page_list = parse_pages(pages, len(pdf.pages))
        eng = None
        if engine in ("auto", "tagged"):
            try:
                eng = TaggedEngine(pdf, page_list)
            except StructTreeMissing:
                if engine == "tagged":
                    raise SystemExit("PDF has no structure tags; use --engine geometry")
        if eng is None:
            eng = GeometryEngine(pdf, page_list)
        blocks = eng.blocks()
        if stitch:
            blocks = stitch_blocks(blocks)
        else:
            for b in blocks:
                if b["type"] == "table":
                    normalize_table(b)
        return {
            "source": pdf_path.rsplit("/", 1)[-1],
            "pages": len(pdf.pages),
            "engine": eng.name,
            "blocks": blocks,
        }


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("-o", "--json", help="nested JSON output (default: <pdf>.json)")
    ap.add_argument("--html", help="readable HTML output")
    ap.add_argument("--sop", help="SOP-flavoured JSON (steps / if-then) output")
    ap.add_argument("--engine", choices=["auto", "tagged", "geometry"], default="auto")
    ap.add_argument("--pages", help="e.g. 1-3,8")
    ap.add_argument("--no-stitch", action="store_true", help="do not merge tables/rows across page breaks")
    args = ap.parse_args(argv)

    doc = extract(args.pdf, args.engine, args.pages, not args.no_stitch)
    out_json = args.json or re.sub(r"\.pdf$", "", args.pdf, flags=re.I) + ".json"
    clean = strip_internal(doc)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    if args.html:
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(render_html(doc))
    if args.sop:
        with open(args.sop, "w", encoding="utf-8") as f:
            json.dump(sop_view(doc), f, ensure_ascii=False, indent=2)

    def count(blocks, depth=0, acc=None):
        acc = acc if acc is not None else Counter()
        for b in blocks:
            if b["type"] == "table":
                acc[depth] += 1
                for r in b["rows"]:
                    for c in r["cells"]:
                        count(c["content"], depth + 1, acc)
        return acc

    c = count(doc["blocks"])
    print(f"{doc['source']}: {doc['pages']} pages, engine={doc['engine']}, "
          f"tables by nesting depth: {dict(sorted(c.items()))} -> {out_json}", file=sys.stderr)


if __name__ == "__main__":
    main()
