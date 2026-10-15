"""Post-processing for detected tables.

merge_vertical_header_cells(): fixes header cells that span several rows
(e.g. 'S. No.' / 'Country' next to a 2-row 'Population' header) but were
reported as one text cell plus an empty cell below it.

A cell is merged with the empty cell directly below it when ALL hold:
  * the upper cell has text, the lower cell is empty
  * both have exactly the same column span
  * both sit on the same fill colour (a visible border/different shading
    between them means they are really separate cells)
  * the lower row contains other cells with text (so it is not an empty
    form-field row)
  * the cell is in the first `max_rows` rows (header area only)
"""
import pdfplumber


def _norm(color):
    if color is None:
        return None
    try:
        if isinstance(color, (int, float)):
            color = (color,)
        return tuple(round(float(v), 2) for v in color)
    except Exception:
        return None


def _fill_at(page, x, y):
    """Fill colour of the top-most filled rectangle containing (x, y)."""
    found = None
    for r in page.rects:                       # drawing order: later = on top
        if (r.get("fill") and r["x0"] <= x <= r["x1"]
                and r["top"] <= y <= r["bottom"]):
            found = r.get("non_stroking_color")
    return _norm(found)


def _center(cell):
    b = cell["bbox"]
    return (b["x0"] + b["x1"]) / 2, (b["top"] + b["bottom"]) / 2


def _rebuild_grid(table):
    n_rows, n_cols = table["row_count"], table["column_count"]
    grid = [[None] * n_cols for _ in range(n_rows)]
    for c in table["cells"]:
        for r in range(c["row"], min(c["row"] + c["rowspan"], n_rows)):
            for k in range(c["column"], min(c["column"] + c["colspan"], n_cols)):
                grid[r][k] = (c["cell_id"] if (r, k) == (c["row"], c["column"])
                              else f"merged:{c['cell_id']}")
    table["grid"] = grid


def _merge_table(page, table, max_rows):
    cells = table["cells"]
    merged = 0
    changed = True
    while changed:
        changed = False
        by_pos = {(c["row"], c["column"]): c for c in cells}
        for up in cells:
            if not up["text"] or up["row"] >= max_rows:
                continue
            down = by_pos.get((up["row"] + up["rowspan"], up["column"]))
            if (not down or down["text"] or down.get("is_spacer")
                    or down["colspan"] != up["colspan"]):
                continue

            rows = range(down["row"], down["row"] + down["rowspan"])
            if not any(c is not down and c["text"] and c["row"] in rows
                       for c in cells):
                continue                      # empty form row, not a header

            f_up = _fill_at(page, *_center(up))
            f_down = _fill_at(page, *_center(down))
            if f_up is None or f_up != f_down:
                continue                      # different shading = separate cells

            up["rowspan"] += down["rowspan"]
            up["bbox"]["bottom"] = down["bbox"]["bottom"]
            up["is_merged"] = True
            cells.remove(down)
            merged += 1
            changed = True
            break
    if merged:
        _rebuild_grid(table)
    return merged


def merge_vertical_header_cells(pdf_path, result, max_rows=4):
    """Merge header cells that span rows. Modifies `result` in place and
    returns the number of merges performed."""
    total = 0
    with pdfplumber.open(pdf_path) as pdf:
        for t in result["tables"]:
            page = pdf.pages[t["page_number"] - 1]
            total += _merge_table(page, t, max_rows)
    return total
