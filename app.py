import os
import tempfile

from flask import Flask, jsonify, request, send_from_directory

from pdf_tables_to_json import pdf_tables_to_json
from table_filter import filter_false_tables
from table_postprocess import merge_vertical_header_cells
from table_extend import extend_header_tables
from table_extend_up import extend_tables_upward

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # 30 MB


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.post("/api/detect")
def detect():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".pdf"):
        return jsonify(error="Upload a PDF file"), 400

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = os.path.join(tmp, "input.pdf")
        out_path = os.path.join(tmp, "out.json")
        f.save(pdf_path)
        try:
            result = pdf_tables_to_json(pdf_path, out_path)
            # drop paragraphs/lists first so they are never extended;
            # pdf_path lets the filter keep tables that have a real drawn grid
            filter_false_tables(result, pdf_path)
            extend_header_tables(pdf_path, result)
            extend_tables_upward(pdf_path, result)
            merge_vertical_header_cells(pdf_path, result)
        except Exception as e:
            return jsonify(error=f"Detection failed: {e}"), 500

    result["source"] = f.filename
    for t in result["tables"]:
        t.pop("grid", None)
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
