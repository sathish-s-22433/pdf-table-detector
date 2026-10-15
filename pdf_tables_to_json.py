import argparse
import json
import logging
import os
import re
from collections import defaultdict
from bisect import bisect_left

import pdfplumber

logging.getLogger("pdfminer").setLevel(logging.ERROR)

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
ENABLE_BORDERLESS = False
ENABLE_BAND_TABLES = True
LOG_REJECTED = True

EDGE_TOL = 2.0
THIN = 2.5
BORDER_COVERAGE = 0.6
MIN_AXIS_COVERAGE = 0.25
MIN_SLOT = 4.0
IOU_DUP = 0.5
PAD = 3

# Validation
MIN_CELLS = 2                    # a table needs at least 2 cells
MIN_TEXT_CELLS = 2               # ...and at least 2 cells with text
MIN_FILL_RULED = 0.25            # only enforced for tables with >= FILL_CHECK_ROWS rows
FILL_CHECK_ROWS = 3
MIN_FILL_BORDERLESS = 0.50
MIN_ROWS_BORDERLESS = 3
MAX_AVG_CELL_CHARS_BORDERLESS = 120

HEADER_SLOT_FACTOR = 2.0

BAND_MIN_WIDTH = 0.6
BAND_MIN_H, BAND_MAX_H = 8, 30
LAST_ROW_GAP = 25
FOOTER_FRACTION = 0.95

LINES_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "edge_min_length": 3,
    "intersection_tolerance": 3,
}

TEXT_SETTINGS = {
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "text_x_tolerance": 3,
    "text_y_tolerance": 3,
    "min_words_vertical": 3,
    "min_words_horizontal": 2,
    "intersection_tolerance": 5,
}


# ----------------------------------------------------------------------
# Geometry helpers
# ----------------------------------------------------------------------
def cluster(values, tol=EDGE_TOL):
    values = sorted(values)
    groups = []
    for v in values:
        if not groups or v - groups[-1][-1] > tol:
            groups.append([v])
        else:
            groups[-1].append(v)
    return [sum(g) / len(g) for g in groups]


def nearest_index(edges, value):
    i = bisect_left(edges, value)
    cands = [j for j in (i - 1, i) if 0 <= j < len(edges)]
    return min(cands, key=lambda j: abs(edges[j] - value))


def iou(a, b):
    x0, t0 = max(a[0], b[0]), max(a[1], b[1])
    x1, t1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or t1 <= t0:
        return 0.0
    inter = (x1 - x0) * (t1 - t0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def intersects(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def union_bbox(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def clamp_bbox(bbox, page):
    return (max(page.bbox[0], bbox[0]), max(page.bbox[1], bbox[1]),
            min(page.bbox[2], bbox[2]), min(page.bbox[3], bbox[3]))


def rnd_bbox(b):
    return {"x0": round(b[0], 2), "top": round(b[1], 2),
            "x1": round(b[2], 2), "bottom": round(b[3], 2)}


def _touching(a, b, gap=4):
    return not (a[2] + gap < b[0] or b[2] + gap < a[0] or
                a[3] + gap < b[1] or b[3] + gap < a[1])


def _inside(a, b, tol=3):
    return (a[0] >= b[0] - tol and a[1] >= b[1] - tol and
            a[2] <= b[2] + tol and a[3] <= b[3] + tol)


# ----------------------------------------------------------------------
# Text extraction
# ----------------------------------------------------------------------
def _chars_in(bbox):
    x0, t, x1, b = bbox

    def keep(obj):
        if obj.get("object_type") != "char":
            return False
        cx = (obj["x0"] + obj["x1"]) / 2
        cy = (obj["top"] + obj["bottom"]) / 2
        return x0 <= cx <= x1 and t <= cy <= b
    return keep


def cell_text(page, bbox):
    bbox = clamp_bbox(bbox, page)
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None
    try:
        txt = page.filter(_chars_in(bbox)).extract_text(
            x_tolerance=2, y_tolerance=3) or ""
    except Exception:
        return None
    return " ".join(txt.split()) or None


def text_extent(page, bbox):
    bbox = clamp_bbox(bbox, page)
    keep = _chars_in(bbox)
    chars = [c for c in page.chars if keep(c) and c["text"].strip()]
    if not chars:
        return None
    return min(c["top"] for c in chars), max(c["bottom"] for c in chars)


class DSU:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, a):
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


# ----------------------------------------------------------------------
# Borders
# ----------------------------------------------------------------------
def _segment(x0, x1, top, bottom, h, v):
    w, ht = x1 - x0, bottom - top
    if ht <= THIN and w > THIN:
        h.append(((top + bottom) / 2, x0, x1))
    elif w <= THIN and ht > THIN:
        v.append(((x0 + x1) / 2, top, bottom))


def collect_borders(page, bbox):
    x0, t, x1, b = bbox
    h, v = [], []

    for ln in page.lines:
        _segment(ln["x0"], ln["x1"], ln["top"], ln["bottom"], h, v)

    for r in page.rects:
        w, ht = r["x1"] - r["x0"], r["bottom"] - r["top"]
        if min(w, ht) <= THIN:
            _segment(r["x0"], r["x1"], r["top"], r["bottom"], h, v)
        elif r.get("stroke") and not r.get("fill"):
            h.append((r["top"], r["x0"], r["x1"]))
            h.append((r["bottom"], r["x0"], r["x1"]))
            v.append((r["x0"], r["top"], r["bottom"]))
            v.append((r["x1"], r["top"], r["bottom"]))

    h = [e for e in h if e[2] >= x0 - PAD and e[1] <= x1 + PAD
         and t - PAD <= e[0] <= b + PAD]
    v = [e for e in v if e[2] >= t - PAD and e[1] <= b + PAD
         and x0 - PAD <= e[0] <= x1 + PAD]

    if len(h) < 2 or len(v) < 2:
        h, v = [], []
        for e in page.edges:
            if e["x1"] < x0 - PAD or e["x0"] > x1 + PAD:
                continue
            if e["bottom"] < t - PAD or e["top"] > b + PAD:
                continue
            if e["orientation"] == "h":
                h.append((e["top"], e["x0"], e["x1"]))
            elif e["orientation"] == "v":
                v.append((e["x0"], e["top"], e["bottom"]))
    return h, v


def covered_ratio(segments, pos, lo, hi, tol=EDGE_TOL * 1.5):
    spans = sorted(
        (max(s, lo), min(e, hi))
        for p, s, e in segments
        if abs(p - pos) <= tol and e > lo and s < hi
    )
    covered, cur_end = 0.0, lo
    for s, e in spans:
        s = max(s, cur_end)
        if e > s:
            covered += e - s
            cur_end = e
    return covered / (hi - lo) if hi > lo else 0.0


def clean_axis(candidates, segments, lo, hi):
    kept = [c for c in candidates
            if covered_ratio(segments, c, lo, hi) >= MIN_AXIS_COVERAGE]
    out = []
    for c in kept:
        if out and c - out[-1] < MIN_SLOT:
            if covered_ratio(segments, c, lo, hi) > covered_ratio(segments, out[-1], lo, hi):
                out[-1] = c
            continue
        out.append(c)
    return out


# ----------------------------------------------------------------------
# Builder A: border-based grid
# ----------------------------------------------------------------------
def build_from_edges(page, bbox):
    h_edges, v_edges = collect_borders(page, bbox)
    if len(h_edges) < 2 or len(v_edges) < 2:
        return None

    x_lo = min(s for _, s, _ in h_edges); x_hi = max(e for _, _, e in h_edges)
    y_lo = min(s for _, s, _ in v_edges); y_hi = max(e for _, _, e in v_edges)

    ys = clean_axis(cluster([p for p, _, _ in h_edges]), h_edges, x_lo, x_hi)
    xs = clean_axis(cluster([p for p, _, _ in v_edges]), v_edges, y_lo, y_hi)
    n_rows, n_cols = len(ys) - 1, len(xs) - 1
    if n_rows < 1 or n_cols < 1:
        return None

    idx = lambda r, c: r * n_cols + c
    dsu = DSU(n_rows * n_cols)

    for r in range(n_rows):
        for c in range(n_cols):
            if c + 1 < n_cols:
                if covered_ratio(v_edges, xs[c + 1], ys[r], ys[r + 1]) < BORDER_COVERAGE:
                    dsu.union(idx(r, c), idx(r, c + 1))
            if r + 1 < n_rows:
                if covered_ratio(h_edges, ys[r + 1], xs[c], xs[c + 1]) < BORDER_COVERAGE:
                    dsu.union(idx(r, c), idx(r + 1, c))

    groups = defaultdict(list)
    for r in range(n_rows):
        for c in range(n_cols):
            groups[dsu.find(idx(r, c))].append((r, c))

    cells = []
    for slots in sorted(groups.values(), key=lambda s: min(s)):
        r0 = min(r for r, _ in slots); r1 = max(r for r, _ in slots)
        c0 = min(c for _, c in slots); c1 = max(c for _, c in slots)
        cb = (xs[c0], ys[r0], xs[c1 + 1], ys[r1 + 1])
        cells.append({
            "row": r0, "column": c0,
            "rowspan": r1 - r0 + 1, "colspan": c1 - c0 + 1,
            "text": cell_text(page, cb),
            "_bbox": cb,
        })
    return {"n_rows": n_rows, "n_cols": n_cols, "cells": cells,
            "xs": xs, "ys": ys}


def split_header_spans(page, built):
    ys = built.get("ys")
    if not ys:
        return
    out = []
    for c in built["cells"]:
        if c["row"] == 0 and c["rowspan"] > 1 and c["text"]:
            r0 = c["row"]
            slot_top, slot_bot = ys[r0], ys[r0 + 1]
            ext = text_extent(page, c["_bbox"])
            if ext:
                text_h = max(ext[1] - ext[0], 1.0)
                if ext[1] <= slot_bot + 1 and (slot_bot - slot_top) <= HEADER_SLOT_FACTOR * text_h:
                    x0, _, x1, _ = c["_bbox"]
                    for r in range(r0, r0 + c["rowspan"]):
                        bb = (x0, ys[r], x1, ys[r + 1])
                        out.append({
                            "row": r, "column": c["column"],
                            "rowspan": 1, "colspan": c["colspan"],
                            "text": cell_text(page, bb),
                            "_bbox": bb,
                        })
                    continue
        out.append(c)
    built["cells"] = sorted(out, key=lambda c: (c["row"], c["column"]))


# ----------------------------------------------------------------------
# Builder B: cell-rectangle based (borderless)
# ----------------------------------------------------------------------
def build_from_cells(page, table):
    raw = [tuple(c) for c in table.cells if c is not None]
    if not raw:
        return None

    xs = cluster([v for c in raw for v in (c[0], c[2])])
    ys = cluster([v for c in raw for v in (c[1], c[3])])
    n_cols, n_rows = len(xs) - 1, len(ys) - 1
    if n_cols < 1 or n_rows < 1:
        return None

    cells, seen, occupied = [], set(), set()
    for bb in sorted(set(raw), key=lambda b: (b[1], b[0], b[3], b[2])):
        key = tuple(round(v, 1) for v in bb)
        if key in seen:
            continue
        seen.add(key)

        c0, c1 = nearest_index(xs, bb[0]), nearest_index(xs, bb[2])
        r0, r1 = nearest_index(ys, bb[1]), nearest_index(ys, bb[3])
        c0 = min(c0, n_cols - 1); r0 = min(r0, n_rows - 1)
        colspan = max(1, min(c1 - c0, n_cols - c0))
        rowspan = max(1, min(r1 - r0, n_rows - r0))
        if (r0, c0) in occupied:
            continue
        for r in range(r0, r0 + rowspan):
            for c in range(c0, c0 + colspan):
                occupied.add((r, c))

        cells.append({
            "row": r0, "column": c0,
            "rowspan": rowspan, "colspan": colspan,
            "text": cell_text(page, bb),
            "_bbox": bb,
        })
    return {"n_rows": n_rows, "n_cols": n_cols, "cells": cells}


# ----------------------------------------------------------------------
# Builder C: shaded header band + numbered rows
# ----------------------------------------------------------------------
def _group_lines(words, tol=3):
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(w["top"] - lines[-1]["top"]) <= tol:
            lines[-1]["words"].append(w)
            lines[-1]["bottom"] = max(lines[-1]["bottom"], w["bottom"])
        else:
            lines.append({"top": w["top"], "bottom": w["bottom"], "words": [w]})
    for ln in lines:
        ln["words"].sort(key=lambda w: w["x0"])
    return lines


def build_band_tables(page, taken):
    W, H = page.width, page.height
    bands = []
    for r in page.rects:
        w, h = r["x1"] - r["x0"], r["bottom"] - r["top"]
        if r.get("fill") and w >= BAND_MIN_WIDTH * W and BAND_MIN_H <= h <= BAND_MAX_H:
            bb = (r["x0"], r["top"], r["x1"], r["bottom"])
            if any(intersects(bb, t) for t in taken):
                continue
            if cell_text(page, bb):
                bands.append(bb)
    bands.sort(key=lambda b: b[1])
    uniq = []
    for b in bands:
        if not uniq or abs(b[1] - uniq[-1][1]) > 2:
            uniq.append(b)
    bands = uniq
    if not bands:
        return []

    words = page.extract_words(x_tolerance=2, y_tolerance=2)
    results = []

    for i, band in enumerate(bands):
        bx0, btop, bx1, bbot = band
        limit = H * FOOTER_FRACTION
        if i + 1 < len(bands):
            limit = min(limit, bands[i + 1][1])
        for t in taken:
            if t[1] >= bbot - 1:
                limit = min(limit, t[1])

        head = [w for w in words
                if btop <= (w["top"] + w["bottom"]) / 2 <= bbot
                and bx0 <= (w["x0"] + w["x1"]) / 2 <= bx1]
        head.sort(key=lambda w: w["x0"])
        labels = []
        for w in head:
            if labels and w["x0"] - labels[-1][1] <= 12:
                labels[-1][1] = w["x1"]
            else:
                labels.append([w["x0"], w["x1"]])
        if len(labels) < 2:
            continue
        edges = [bx0]
        for prev, nxt in zip(labels, labels[1:]):
            width = nxt[1] - nxt[0]
            edges.append(max(prev[1] + 1, nxt[0] - width / 2))
        edges.append(bx1)
        n_cols = len(edges) - 1

        body = [w for w in words
                if bbot - 1 <= w["top"] and w["bottom"] <= limit
                and bx0 <= w["x0"] <= bx1]
        lines = _group_lines(body)
        left_limit = bx0 + 0.2 * (bx1 - bx0)
        starts = [k for k, ln in enumerate(lines)
                  if ln["words"][0]["x0"] <= left_limit
                  and re.match(r"^\d+[.)]$", ln["words"][0]["text"])]
        if len(starts) < 2:
            continue

        last = starts[-1]
        end_bottom = lines[last]["bottom"]
        for k in range(last + 1, len(lines)):
            if lines[k]["top"] - end_bottom > LAST_ROW_GAP:
                break
            end_bottom = lines[k]["bottom"]

        row_tops = [bbot] + [lines[k]["top"] - 3 for k in starts[1:]]
        row_bottoms = row_tops[1:] + [end_bottom + 2]

        cells = []
        for c in range(n_cols):
            bb = (edges[c], btop, edges[c + 1], bbot)
            cells.append({"row": 0, "column": c, "rowspan": 1, "colspan": 1,
                          "text": cell_text(page, bb), "_bbox": bb})
        for r, (t, b) in enumerate(zip(row_tops, row_bottoms), start=1):
            for c in range(n_cols):
                bb = (edges[c], t, edges[c + 1], b)
                cells.append({"row": r, "column": c, "rowspan": 1, "colspan": 1,
                              "text": cell_text(page, bb), "_bbox": bb})

        bbox = (bx0, btop, bx1, row_bottoms[-1])
        results.append((bbox, {"n_rows": len(starts) + 1, "n_cols": n_cols,
                               "cells": cells}))
    return results


# ----------------------------------------------------------------------
# Finalize + validation
# ----------------------------------------------------------------------
def finalize(built, page_num, idx, strategy):
    n_rows, n_cols, cells = built["n_rows"], built["n_cols"], built["cells"]
    if not cells:
        return None

    bbox = cells[0]["_bbox"]
    for c in cells[1:]:
        bbox = union_bbox(bbox, c["_bbox"])

    grid = [[None] * n_cols for _ in range(n_rows)]
    out_cells = []
    for i, c in enumerate(cells, start=1):
        cid = f"p{page_num}_t{idx}_c{i}"
        is_spacer = (n_rows > 1 and c["text"] is None
                     and c["colspan"] == n_cols and n_cols > 1)
        out_cells.append({
            "cell_id": cid,
            "row": c["row"], "column": c["column"],
            "rowspan": c["rowspan"], "colspan": c["colspan"],
            "is_merged": c["rowspan"] > 1 or c["colspan"] > 1,
            "is_spacer": is_spacer,
            "text": c["text"],
            "bbox": rnd_bbox(c["_bbox"]),
        })
        for r in range(c["row"], min(c["row"] + c["rowspan"], n_rows)):
            for col in range(c["column"], min(c["column"] + c["colspan"], n_cols)):
                grid[r][col] = cid if (r, col) == (c["row"], c["column"]) else f"merged:{cid}"

    # header only makes sense with more than one row
    first_row = [c for c in out_cells if c["row"] == 0]
    header_rows = 1 if (n_rows > 1 and first_row and all(c["text"] for c in first_row)) else 0
    filled = sum(1 for c in out_cells if c["text"])

    return {
        "table_id": f"page_{page_num}_table_{idx}",
        "page_number": page_num,
        "strategy": strategy,
        "row_count": n_rows,
        "column_count": n_cols,
        "header_rows": header_rows,
        "fill_ratio": round(filled / len(out_cells), 3),
        "bbox": rnd_bbox(bbox),
        "cells": out_cells,
        "grid": grid,
    }


def reject_reason(tbl):
    """None if valid, otherwise the reason. Single-row/column tables are allowed."""
    n_rows, n_cols = tbl["row_count"], tbl["column_count"]
    if n_rows * n_cols < MIN_CELLS or (n_rows < 2 and n_cols < 2):
        return "single box / divider (only 1 cell)"

    texts = [c["text"] for c in tbl["cells"] if c["text"]]
    if len(texts) < MIN_TEXT_CELLS:
        return f"fewer than {MIN_TEXT_CELLS} cells with text"

    strategy = tbl["strategy"]
    if strategy == "band":
        return None

    borderless = strategy in ("text", "cells")
    if borderless:
        if n_rows < MIN_ROWS_BORDERLESS:
            return "borderless with < 3 rows"
        if tbl["fill_ratio"] < MIN_FILL_BORDERLESS:
            return f"fill ratio {tbl['fill_ratio']} < {MIN_FILL_BORDERLESS}"
        if sum(len(t) for t in texts) / len(texts) > MAX_AVG_CELL_CHARS_BORDERLESS:
            return "looks like a paragraph"
    elif n_rows >= FILL_CHECK_ROWS and tbl["fill_ratio"] < MIN_FILL_RULED:
        # empty form fields are normal in short tables, so only large ones are checked
        return f"fill ratio {tbl['fill_ratio']} < {MIN_FILL_RULED}"
    return None


def group_rowspan_records(table):
    cells = [c for c in table["cells"] if not c["is_spacer"]]
    records = []
    for p in cells:
        if (p["column"] != 0 or p["rowspan"] <= 1
                or table["column_count"] < 2 or not p["text"]):
            continue
        rows = range(p["row"], p["row"] + p["rowspan"])
        children = [
            c["text"] for c in sorted(cells, key=lambda c: (c["row"], c["column"]))
            if c["column"] > 0 and c["row"] in rows and c["text"]
        ]
        if len(children) >= 2:
            records.append({"parent": p["text"], "children": children})
    return records


# ----------------------------------------------------------------------
# Region detection
# ----------------------------------------------------------------------
def detect_regions(page):
    regions = []
    try:
        for t in page.find_tables(table_settings=LINES_SETTINGS):
            regions.append([tuple(t.bbox), "edges", t])
    except Exception as e:
        logging.warning("lines detection failed: %s", e)

    merged = []
    for reg in sorted(regions, key=lambda r: (r[0][1], r[0][0])):
        for m in merged:
            if iou(reg[0], m[0]) > 0 or _touching(reg[0], m[0]):
                m[0] = union_bbox(reg[0], m[0])
                break
        else:
            merged.append(reg)
    regions = merged

    if ENABLE_BORDERLESS:
        try:
            for t in page.find_tables(table_settings=TEXT_SETTINGS):
                tb = tuple(t.bbox)
                if all(iou(tb, r[0]) < IOU_DUP and not _inside(tb, r[0]) for r in regions):
                    regions.append([tb, "text", t])
        except Exception as e:
            logging.warning("text detection failed: %s", e)

    regions.sort(key=lambda r: (r[0][1], r[0][0]))
    return regions


def mark_continuations(tables, tol=6.0):
    def xs(t):
        return sorted({round(c["bbox"]["x0"]) for c in t["cells"]})

    for prev, cur in zip(tables, tables[1:]):
        if cur["page_number"] != prev["page_number"] + 1 or cur["header_rows"]:
            continue
        a, b = xs(prev), xs(cur)
        if len(a) == len(b) and all(abs(p - q) <= tol for p, q in zip(a, b)):
            cur["continues_from"] = prev["table_id"]


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def pdf_tables_to_json(pdf_path, output_json_path=None):
    """Detect tables in `pdf_path`. Writes JSON to `output_json_path` if given.
    Always returns the result dict."""
    result = {
        "source": pdf_path,
        "coordinate_system": {
            "unit": "PDF points",
            "origin": "top-left",
            "bbox_order": ["x0", "top", "x1", "bottom"],
        },
        "tables": [],
    }

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            idx = 0
            taken = []

            for bbox, strategy, ptable in detect_regions(page):
                if strategy == "edges":
                    built = build_from_edges(page, bbox)
                    if built:
                        split_header_spans(page, built)
                    else:
                        built, strategy = build_from_cells(page, ptable), "cells"
                else:
                    built = build_from_cells(page, ptable)
                if not built:
                    continue

                tbl = finalize(built, page_num, idx + 1, strategy)
                if not tbl:
                    continue
                reason = reject_reason(tbl)
                if reason:
                    if LOG_REJECTED:
                        print(f"[page {page_num}] rejected {strategy} region "
                              f"{tbl['bbox']}: {reason}")
                    continue

                idx += 1
                tbl["records"] = group_rowspan_records(tbl)
                result["tables"].append(tbl)
                taken.append((tbl["bbox"]["x0"], tbl["bbox"]["top"],
                              tbl["bbox"]["x1"], tbl["bbox"]["bottom"]))

            if ENABLE_BAND_TABLES:
                for bbox, built in build_band_tables(page, taken):
                    tbl = finalize(built, page_num, idx + 1, "band")
                    if not tbl:
                        continue
                    reason = reject_reason(tbl)
                    if reason:
                        if LOG_REJECTED:
                            print(f"[page {page_num}] rejected band region: {reason}")
                        continue
                    idx += 1
                    tbl["records"] = []
                    result["tables"].append(tbl)
                    taken.append(bbox)

    mark_continuations(result["tables"])

    if output_json_path:
        with open(output_json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
    return result


def _parse_args():
    p = argparse.ArgumentParser(description="Detect tables in a PDF and write them as JSON.")
    p.add_argument("pdf", help="path to the input PDF file")
    p.add_argument("-o", "--output",
                   help="output JSON path (default: <pdf name>_tables.json next to the PDF)")
    p.add_argument("--borderless", action="store_true",
                   help="also try to detect borderless tables")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if not os.path.isfile(args.pdf):
        raise SystemExit(f"PDF not found: {args.pdf}")
    if args.borderless:
        ENABLE_BORDERLESS = True
    out = args.output or os.path.splitext(args.pdf)[0] + "_tables.json"
    res = pdf_tables_to_json(args.pdf, out)
    print(f"{len(res['tables'])} table(s) written to {out}")
