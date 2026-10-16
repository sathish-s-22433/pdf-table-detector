"""Remove false-positive tables: paragraphs and bullet lists.

Text blocks sometimes get ruled/underline-like artefacts (justified text, line
rules, shaded rows) that make the detector report them as tables, usually with
a single column where every row is one line of a sentence.

But a REAL table can also hold long sentences and numbered lists inside its
cells (e.g. a 'Packet Switching | Circuit Switching' comparison). What tells
them apart is the ruling: a real table has drawn lines that form a grid,
especially a vertical divider between columns running most of the table height.

Order of rules (first match wins)
  0. real grid              an inner vertical divider + horizontal rules drawn
                            across the table  ->  ALWAYS KEEP
  1. single column          one column of text lines = paragraph or list
  2. bullet list            2 columns, first column only bullet glyphs
  3. long prose, few cols   <= 2 columns, very long text cells

The grid check now runs FIRST and joins line SEGMENTS: Word/Office PDFs draw a
column divider as many short pieces (one per row), so no single piece spans
the table. Pieces on the same x position are merged before measuring.

Rejected tables are listed in result["rejected_tables"] with the reason.
"""
import re

import pdfplumber

MIN_COLUMNS = 2
MAX_PROSE_COLS = 2
PROSE_AVG_CHARS = 70           # average characters per text cell
PROSE_WORDS_PER_CELL = 9       # ... or average words per cell

# real-grid evidence
SPAN_FRACTION = 0.6            # merged divider/rule must cover this share
DIVIDER_MARGIN = 10.0          # divider must be this far from left/right side
MIN_H_RULES = 2                # horizontal rules spanning the table width
POS_TOL = 2.5                  # lines this close share one position
GAP_TOL = 4.0                  # segments closer than this are joined

BULLET_RE = re.compile(r"^[\u2022\u00b7\u25aa\u25cf\u25cb\u25a0\u25a1\u25e6\u2023\u2043\-\u2013\u2014*o]$")


def _texts(table, column=None):
    return [c["text"] for c in table["cells"]
            if c["text"] and (column is None or c["column"] == column)]


def _group_by_position(segments):
    """segments: (pos, start, end). Group those whose pos is within POS_TOL."""
    groups = []
    for pos, s, e in sorted(segments):
        if groups and pos - groups[-1]["pos"] <= POS_TOL:
            g = groups[-1]
            g["spans"].append((s, e))
        else:
            groups.append({"pos": pos, "spans": [(s, e)]})
    return groups


def _joined_length(spans, lo, hi):
    """Total covered length inside [lo, hi] after joining nearby spans."""
    covered, end = 0.0, lo
    for s, e in sorted((max(s, lo), min(e, hi)) for s, e in spans):
        if e <= s:
            continue
        s = max(s, end - GAP_TOL if end > lo else s)
        if e > end:
            covered += e - max(s, end) if s < end else e - s
            end = e
    return covered


def has_real_grid(page, table):
    """True when drawn lines form a grid: an inner vertical divider covering
    most of the table height AND >= MIN_H_RULES horizontal rules covering most
    of its width. Segments on the same line are joined first."""
    b = table["bbox"]
    x0, x1, top, bottom = b["x0"], b["x1"], b["top"], b["bottom"]
    height, width = bottom - top, x1 - x0
    if height <= 0 or width <= 0:
        return False
    pad = 4.0

    v_seg, h_seg = [], []
    for e in page.edges:
        if e["orientation"] == "v":
            if x0 + DIVIDER_MARGIN <= e["x0"] <= x1 - DIVIDER_MARGIN:
                v_seg.append((e["x0"], e["top"], e["bottom"]))
        elif e["orientation"] == "h":
            if top - pad <= e["top"] <= bottom + pad:
                h_seg.append((e["top"], e["x0"], e["x1"]))

    dividers = [g for g in _group_by_position(v_seg)
                if _joined_length(g["spans"], top, bottom) >= SPAN_FRACTION * height]
    rules = [g for g in _group_by_position(h_seg)
             if _joined_length(g["spans"], x0, x1) >= SPAN_FRACTION * width]
    return bool(dividers) and len(rules) >= MIN_H_RULES


def reject_reason(table, page=None):
    """Reason string if `table` is really a paragraph/list, else None."""
    n_cols = table["column_count"]

    # 0. drawn grid lines = genuine table, whatever the cell text looks like
    if page is not None and has_real_grid(page, table):
        return None

    # 1. one column = text lines, not a table
    if n_cols < MIN_COLUMNS:
        return "single column (paragraph or list)"

    # 2. bullet column + text column
    if n_cols == 2:
        first = _texts(table, 0)
        if first and all(BULLET_RE.match(t.strip()) for t in first):
            return "bullet list"

    # 3. few columns of long sentences
    if n_cols <= MAX_PROSE_COLS:
        texts = _texts(table)
        if texts:
            avg_chars = sum(len(t) for t in texts) / len(texts)
            avg_words = sum(len(t.split()) for t in texts) / len(texts)
            if avg_chars >= PROSE_AVG_CHARS or avg_words >= PROSE_WORDS_PER_CELL:
                return "long prose in few columns"
    return None


def filter_false_tables(result, pdf_path=None):
    """Drop paragraph/list false positives from `result` in place.
    Pass `pdf_path` so border evidence can be used. Returns the number of
    tables removed."""
    kept, rejected = [], []
    pdf = pdfplumber.open(pdf_path) if pdf_path else None
    try:
        for t in result["tables"]:
            page = pdf.pages[t["page_number"] - 1] if pdf else None
            reason = reject_reason(t, page)
            if reason:
                rejected.append({"table_id": t["table_id"],
                                 "page_number": t["page_number"],
                                 "bbox": t["bbox"], "reason": reason})
                print(f"[page {t['page_number']}] filtered {t['table_id']}: {reason}")
            else:
                kept.append(t)
    finally:
        if pdf:
            pdf.close()
    result["tables"] = kept
    if rejected:
        result["rejected_tables"] = rejected
    return len(rejected)
