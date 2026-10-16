"""Remove false-positive tables: paragraphs and bullet lists.

Text blocks sometimes get ruled/underline-like artefacts (justified text, line
rules, shaded rows) that make the detector report them as tables, usually with
a single column where every row is one line of a sentence.

But a REAL table can also hold long sentences and numbered lists inside its
cells (e.g. a 'Packet Switching | Circuit Switching' comparison). What tells
them apart is the ruling: a real table has drawn lines that form a grid,
especially a vertical divider between columns running most of the table height.

filter_false_tables() therefore uses two kinds of evidence:
  * text rules   (single column, bullet column, long prose in few columns)
  * border rules (internal vertical divider + horizontal rules = real grid)

A table with a real grid is never rejected by the bullet/prose rules.
Rejected tables are listed in result["rejected_tables"] with the reason.

Rules
  1. single column          one column of text lines = paragraph or list
                            (always rejected, even if boxed)
  2. bullet list            2 columns, first column only bullet glyphs
                            (skipped when a real grid is present)
  3. long prose, few cols   <= 2 columns, very long text cells
                            (skipped when a real grid is present)
"""
import re

import pdfplumber

MIN_COLUMNS = 2
MAX_PROSE_COLS = 2
PROSE_AVG_CHARS = 70           # average characters per text cell
PROSE_WORDS_PER_CELL = 9       # ... or average words per cell

# real-grid evidence
SPAN_FRACTION = 0.7            # a divider must cover this share of the table
DIVIDER_MARGIN = 10.0          # divider must be this far from the left/right side
MIN_H_RULES = 2                # horizontal rules spanning the table width
POS_TOL = 2.5

BULLET_RE = re.compile(r"^[\u2022\u00b7\u25aa\u25cf\u25cb\u25a0\u25a1\u25e6\u2023\u2043\-\u2013\u2014*o]$")


def _texts(table, column=None):
    return [c["text"] for c in table["cells"]
            if c["text"] and (column is None or c["column"] == column)]


def _distinct(values, tol=POS_TOL):
    out = []
    for v in sorted(values):
        if not out or v - out[-1] > tol:
            out.append(v)
    return out


def has_real_grid(page, table):
    """True when drawn lines form a grid: at least one vertical divider
    strictly inside the table spanning most of its height, and at least
    MIN_H_RULES horizontal rules spanning most of its width."""
    b = table["bbox"]
    x0, x1, top, bottom = b["x0"], b["x1"], b["top"], b["bottom"]
    height, width = bottom - top, x1 - x0
    if height <= 0 or width <= 0:
        return False
    pad = 4.0

    v_inner, h_rules = [], []
    for e in page.edges:
        if e["orientation"] == "v":
            if e["x0"] < x0 - pad or e["x0"] > x1 + pad:
                continue
            lo, hi = max(e["top"], top), min(e["bottom"], bottom)
            if hi - lo >= SPAN_FRACTION * height and \
                    x0 + DIVIDER_MARGIN <= e["x0"] <= x1 - DIVIDER_MARGIN:
                v_inner.append(e["x0"])
        elif e["orientation"] == "h":
            if e["top"] < top - pad or e["top"] > bottom + pad:
                continue
            lo, hi = max(e["x0"], x0), min(e["x1"], x1)
            if hi - lo >= SPAN_FRACTION * width:
                h_rules.append(e["top"])

    return bool(_distinct(v_inner)) and len(_distinct(h_rules)) >= MIN_H_RULES


def reject_reason(table, page=None):
    """Reason string if `table` is really a paragraph/list, else None."""
    n_cols = table["column_count"]

    # 1. one column = text lines, not a table
    if n_cols < MIN_COLUMNS:
        return "single column (paragraph or list)"

    # Drawn grid lines mean a genuine table, whatever the cell text looks like.
    if page is not None and has_real_grid(page, table):
        return None

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
            else:
                kept.append(t)
    finally:
        if pdf:
            pdf.close()
    result["tables"] = kept
    if rejected:
        result["rejected_tables"] = rejected
    return len(rejected)
