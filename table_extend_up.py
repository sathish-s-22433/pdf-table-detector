"""Extend tables UPWARD over shaded header rows that have no borders.

Some tables draw ruled borders only for the body rows. The header row(s) are
just filled bands (e.g. 'Subject Code | Subjects of the Test', or
'CHEMICAL SCIENCES (701) | PART A | PART B ...') with no stroked lines, so
ruled-line detection starts below them and the table is only partly found.

extend_tables_upward() looks at filled rectangles directly above each table:
  * the rectangles must touch the current top edge of the table
  * together they must cover most of the table width
  * they must be a plausible row height
Each such band becomes a new row at the top of the table, using the table's
existing column edges. The process repeats upward, then trailing bands without
any text are dropped.
"""
import pdfplumber

from pdf_tables_to_json import cell_text, rnd_bbox
from table_postprocess import _rebuild_grid

TOUCH_TOL = 5.0      # band bottom must be within this of the table top
MIN_H, MAX_H = 6.0, 80.0
MIN_COVER = 0.8      # share of the table width the band must cover
X_PAD = 8.0
EDGE_TOL = 2.5
MAX_UP_ROWS = 8


def _edges(cells):
    vals = sorted(v for c in cells for v in (c["bbox"]["x0"], c["bbox"]["x1"]))
    groups = []
    for v in vals:
        if groups and v - groups[-1][-1] <= EDGE_TOL:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def _coverage(rects, x0, x1):
    spans = sorted((max(x0, r["x0"]), min(x1, r["x1"])) for r in rects)
    covered, end = 0.0, x0
    for a, b in spans:
        a = max(a, end)
        if b > a:
            covered += b - a
            end = b
    return covered


def _band_above(page, top, x0, x1):
    """(band_top, band_bottom) of the filled band touching `top`, or None."""
    groups = {}
    for r in page.rects:
        if not r.get("fill"):
            continue
        h = r["bottom"] - r["top"]
        if h < MIN_H or h > MAX_H:
            continue
        if abs(r["bottom"] - top) > TOUCH_TOL:
            continue
        if r["x1"] < x0 - X_PAD or r["x0"] > x1 + X_PAD:
            continue
        groups.setdefault(round(r["top"] / 2), []).append(r)

    best, best_cov = None, 0.0
    for g in groups.values():
        cov = _coverage(g, x0, x1)
        if cov >= MIN_COVER * (x1 - x0) and cov > best_cov:
            best, best_cov = g, cov
    if not best:
        return None
    return min(r["top"] for r in best), top


def _extend_up(page, t):
    cells = t["cells"]
    if not cells:
        return 0
    edges = _edges(cells)
    n_cols = len(edges) - 1
    if n_cols != t["column_count"] or n_cols < 1:
        return 0
    x0, x1 = edges[0], edges[-1]

    bands, top = [], t["bbox"]["top"]
    while len(bands) < MAX_UP_ROWS:
        b = _band_above(page, top, x0, x1)
        if not b or b[0] >= top - 1:
            break
        bands.append(b)
        top = b[0]
    if not bands:
        return 0

    bands.reverse()                       # top -> bottom
    new_rows = []
    for btop, bbot in bands:
        row = [(edges[c], btop, edges[c + 1], bbot) for c in range(n_cols)]
        texts = [cell_text(page, bb) for bb in row]
        new_rows.append((row, texts))

    while new_rows and not any(new_rows[0][1]):   # drop empty top bands
        new_rows.pop(0)
    if not new_rows:
        return 0

    k = len(new_rows)
    for c in cells:
        c["row"] += k

    prefix = cells[0]["cell_id"].rsplit("_c", 1)[0]
    nxt = len(cells) + 1
    added = []
    for r, (row, texts) in enumerate(new_rows):
        for c, (bb, txt) in enumerate(zip(row, texts)):
            added.append({
                "cell_id": f"{prefix}_c{nxt}",
                "row": r, "column": c, "rowspan": 1, "colspan": 1,
                "is_merged": False, "is_spacer": False,
                "text": txt, "bbox": rnd_bbox(bb),
            })
            nxt += 1
    cells[:0] = added
    cells.sort(key=lambda c: (c["row"], c["column"]))

    t["row_count"] += k
    t["bbox"]["top"] = round(new_rows[0][0][0][1], 2)
    first = [c for c in cells if c["row"] == 0]
    t["header_rows"] = 1 if first and all(c["text"] for c in first) else t.get("header_rows", 0)
    t["fill_ratio"] = round(sum(1 for c in cells if c["text"]) / len(cells), 3)
    t["extended_up_rows"] = k
    _rebuild_grid(t)
    return k


def extend_tables_upward(pdf_path, result):
    """Add borderless shaded header rows above detected tables.
    Modifies `result` in place and returns the number of rows added."""
    added = 0
    with pdfplumber.open(pdf_path) as pdf:
        for t in result["tables"]:
            page = pdf.pages[t["page_number"] - 1]
            added += _extend_up(page, t)
    return added
