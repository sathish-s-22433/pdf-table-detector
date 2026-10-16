"""Extend header-only tables downward over borderless body rows.

Some tables (e.g. a 'Table of Content') only draw their header bands. The
body rows have no borders or shading, so ruled-line detection stops after the
header. extend_header_tables() looks at the text directly below such a table
and, when it forms aligned rows and columns, adds those rows to the table.

Steps for each short table (<= MAX_HEADER_ROWS rows):
  1. collect words below it, inside its horizontal range, until a large gap
     (or the next table / the page footer)
  2. group words into lines
  3. find columns = x-ranges of words separated by whitespace gaps
  4. start a new row for every line that has text outside the widest column;
     a line with text only in the widest column is a wrapped continuation
  5. cut contiguous row boxes and read the text of every row x column cell
  6. re-map the existing header cells onto the new column grid
"""
import pdfplumber

from pdf_tables_to_json import cell_text, rnd_bbox
from table_postprocess import _rebuild_grid

MAX_HEADER_ROWS = 4       # only tables this short are extended
MIN_BODY_ROWS = 3         # need at least this many aligned rows below
MIN_COL_GAP = 10.0        # whitespace (points) that separates two columns
MAX_START_GAP = 35.0      # max gap between the table and its first body line
MAX_ROW_GAP = 40.0        # a larger vertical gap ends the body
LINE_TOL = 5.0            # words within this vertical distance share a line
X_PAD = 6.0               # body text may sit this far outside the table box
FOOTER_FRACTION = 0.95    # ignore text in the page footer


def _lines(words):
    lines = []
    for w in sorted(words, key=lambda w: ((w["top"] + w["bottom"]) / 2, w["x0"])):
        cy = (w["top"] + w["bottom"]) / 2
        if lines and abs(cy - lines[-1]["cy"]) <= LINE_TOL:
            ln = lines[-1]
            ln["words"].append(w)
            ln["top"] = min(ln["top"], w["top"])
            ln["bottom"] = max(ln["bottom"], w["bottom"])
        else:
            lines.append({"cy": cy, "top": w["top"], "bottom": w["bottom"],
                          "words": [w]})
    return lines


def _columns(words, x_lo, x_hi):
    """Column boundaries from whitespace valleys in the x-projection."""
    merged = []
    for a, b in sorted((w["x0"], w["x1"]) for w in words):
        if merged and a - merged[-1][1] <= MIN_COL_GAP:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    if len(merged) < 2:
        return None
    return ([x_lo]
            + [(p[1] + n[0]) / 2 for p, n in zip(merged, merged[1:])]
            + [x_hi])


def _col_of(edges, w):
    cx = (w["x0"] + w["x1"]) / 2
    for i in range(len(edges) - 1):
        if cx < edges[i + 1]:
            return i
    return len(edges) - 2


def _remap_cell(cell, edges):
    """Place an existing (header) cell on the new column grid."""
    n = len(edges) - 1
    b = cell["bbox"]
    mids = [(edges[i] + edges[i + 1]) / 2 for i in range(n)]
    cover = [i for i in range(n) if b["x0"] - 1 <= mids[i] <= b["x1"] + 1]
    if not cover:
        cx = (b["x0"] + b["x1"]) / 2
        cover = [min(range(n), key=lambda i: abs(mids[i] - cx))]
    cell["column"] = cover[0]
    cell["colspan"] = cover[-1] - cover[0] + 1
    cell["is_merged"] = cell["rowspan"] > 1 or cell["colspan"] > 1


def _extend(page, t, tabs, words):
    b = t["bbox"]
    limit = page.height * FOOTER_FRACTION
    for o in tabs:
        if o is not t and o["bbox"]["top"] >= b["bottom"] - 1:
            limit = min(limit, o["bbox"]["top"])

    body = [w for w in words
            if w["top"] >= b["bottom"] - 1 and w["bottom"] <= limit
            and w["x0"] >= b["x0"] - X_PAD and w["x1"] <= b["x1"] + X_PAD]

    kept, prev = [], b["bottom"]
    for ln in _lines(body):
        if ln["top"] - prev > (MAX_ROW_GAP if kept else MAX_START_GAP):
            break
        kept.append(ln)
        prev = ln["bottom"]
    if not kept:
        return 0

    edges = _columns([w for ln in kept for w in ln["words"]], b["x0"], b["x1"])
    if not edges:
        return 0
    n_cols = len(edges) - 1
    wide = max(range(n_cols), key=lambda i: edges[i + 1] - edges[i])

    # rows: a line with text outside the widest column starts a new row
    rows = []
    for ln in kept:
        cols = {_col_of(edges, w) for w in ln["words"]}
        if rows and cols <= {wide}:
            rows[-1]["bottom"] = ln["bottom"]
        else:
            rows.append({"top": ln["top"], "bottom": ln["bottom"]})
    if len(rows) < MIN_BODY_ROWS:
        return 0

    tops = [b["bottom"]] + [(p["bottom"] + n["top"]) / 2
                            for p, n in zip(rows, rows[1:])]
    bottoms = tops[1:] + [rows[-1]["bottom"] + 2]

    cells = t["cells"]
    for c in cells:
        _remap_cell(c, edges)

    prefix = cells[0]["cell_id"].rsplit("_c", 1)[0]
    nxt = len(cells) + 1
    base_row = t["row_count"]
    for r, (top, bot) in enumerate(zip(tops, bottoms)):
        for c in range(n_cols):
            bb = (edges[c], top, edges[c + 1], bot)
            cells.append({
                "cell_id": f"{prefix}_c{nxt}",
                "row": base_row + r, "column": c,
                "rowspan": 1, "colspan": 1,
                "is_merged": False, "is_spacer": False,
                "text": cell_text(page, bb),
                "bbox": rnd_bbox(bb),
            })
            nxt += 1

    t["row_count"] = base_row + len(rows)
    t["column_count"] = n_cols
    t["bbox"]["bottom"] = round(bottoms[-1], 2)
    t["bbox"]["x0"] = round(min(t["bbox"]["x0"], edges[0]), 2)
    t["bbox"]["x1"] = round(max(t["bbox"]["x1"], edges[-1]), 2)
    t["strategy"] = "extended"
    t["extended_rows"] = len(rows)
    t["fill_ratio"] = round(sum(1 for c in cells if c["text"]) / len(cells), 3)
    t["records"] = []
    _rebuild_grid(t)
    return len(rows)


def extend_header_tables(pdf_path, result):
    """Extend short header-only tables over the borderless rows below them.
    Modifies `result` in place and returns the number of rows added."""
    added = 0
    by_page = {}
    for t in result["tables"]:
        by_page.setdefault(t["page_number"], []).append(t)

    with pdfplumber.open(pdf_path) as pdf:
        for pn, tabs in by_page.items():
            page = pdf.pages[pn - 1]
            words = None
            for t in tabs:
                if t["row_count"] > MAX_HEADER_ROWS:
                    continue
                if words is None:
                    words = page.extract_words(x_tolerance=2, y_tolerance=2)
                added += _extend(page, t, tabs, words)
    return added
