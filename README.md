# pdf-table-detector

Web app: upload a PDF, render its pages, click **Detect Tables**, and see tables, rows, columns and cells drawn on the pages.

## Run locally

```bash
pip install -r requirements.txt
python app.py   # http://localhost:8000
```

## Required file

Add your detection script as `pdf_tables_to_json.py` in the repo root. `app.py` imports its `pdf_tables_to_json(pdf_path, output_json_path)` function, which must return the result dict.

## Deploy

Use the included `Dockerfile` on Render, Railway, Fly.io or Hugging Face Spaces (Docker SDK, app_port 8000).
