"""Remove false-positive tables: paragraphs and bullet lists.

Text blocks sometimes get ruled/underline-like artefacts (justified text, line
rules, shaded rows) that make the detector report them as tables, usually with
a single column where every row is one line of a sentence.

filter_false_tables() drops a table when it matches any rule below. Rejected
tables are listed in result["rejected_tables"] with the reason, so wrong
rejections can be reviewed.

Rules
  1. single column          a table needs >= 2 columns; one column of text
                            lines is a paragraph or a list
  2. bullet list            2 columns where the first column holds only bullet
                            glyphs and the second holds the text
  3. long prose, few cols   <= 2 columns and the average text cell is very long
                            (sentences, not data)
"""
import re

MIN_COLUMNS = 2
MAX_PROSE_COLS = 2
PROSE_AVG_CHARS = 70           # average characters per text cell
PROSE_WORDS_PER_CELL = 9       # ... or average words per cell

BULLET_RE = re.compile(r"^[\u2022\u00b7\u25aa\u25cf\u25cb\u25a0\u25a1\u25e6\u2023\u2043\-\u2013\u2014*o]$")


def _texts(table, column=None):
    return [c["text"] for c in table["cells"]
            if c["text"] and (column is None or c["column"] == column)]


def reject_reason(table):
    """Reason string if `table` is really a paragraph/list, else None."""
    n_cols = table["column_count"]

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


def filter_false_tables(result):
    """Drop paragraph/list false positives from `result` in place.
    Returns the number of tables removed."""
    kept, rejected = [], []
    for t in result["tables"]:
        reason = reject_reason(t)
        if reason:
            rejected.append({"table_id": t["table_id"],
                             "page_number": t["page_number"],
                             "bbox": t["bbox"], "reason": reason})
        else:
            kept.append(t)
    result["tables"] = kept
    if rejected:
        result["rejected_tables"] = rejected
    return len(rejected)
