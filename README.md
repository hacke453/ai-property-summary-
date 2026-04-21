# AI Generalist – DDR Report Generator

This project generates a **Main DDR (Detailed Diagnostic Report)** from:
- an **Inspection Report PDF** (site observations)
- a **Thermal Report PDF** (thermal readings / thermal images)

It extracts **text + embedded images** from the PDFs and produces:
- `ddr.md` (client-friendly structured report)
- `ddr.html` (same report, easy to share/print)
- `assets/` (extracted images)
- `extracted/inspection.json` and `extracted/thermal.json` (intermediate structured data)

## Prerequisites
- Windows 10/11
- Python 3.10+ (recommended 3.11)

## Setup (PowerShell)

```powershell
cd "C:\Users\shubh\Desktop\AI generalist"
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
pip install -r requirements.txt
```

## Run (your provided PDFs)

```powershell
cd "C:\Users\shubh\Desktop\AI generalist"
.\.venv\Scripts\Activate.ps1

$inspection = "C:\Users\shubh\AppData\Roaming\Cursor\User\workspaceStorage\1fac15e83e88baca15f8084954e18d7e\pdfs\5183d038-c5c3-431c-b839-378b1466fcd6\Sample Report.pdf"
$thermal = "C:\Users\shubh\AppData\Roaming\Cursor\User\workspaceStorage\1fac15e83e88baca15f8084954e18d7e\pdfs\f52bd0f5-e2d5-4d73-b6ef-f5ade1d2e5b2\Thermal Images.pdf"

py .\scripts\generate_ddr.py --inspection "$inspection" --thermal "$thermal" --outdir .\out
```

Outputs will be in `.\out\`.

## Create a single submission zip (recommended)

This re-generates `.\out\` and creates `.\submission_bundle.zip` containing:
- `out/` (DDR + assets + extracted JSON)
- `project/` (minimal code to reproduce: `requirements.txt`, `README.md`, `scripts/`)

```powershell
cd "C:\Users\shubh\Desktop\AI generalist"
.\.venv\Scripts\Activate.ps1

$inspection = "C:\Users\shubh\AppData\Roaming\Cursor\User\workspaceStorage\1fac15e83e88baca15f8084954e18d7e\pdfs\5183d038-c5c3-431c-b839-378b1466fcd6\Sample Report.pdf"
$thermal    = "C:\Users\shubh\AppData\Roaming\Cursor\User\workspaceStorage\1fac15e83e88baca15f8084954e18d7e\pdfs\f52bd0f5-e2d5-4d73-b6ef-f5ade1d2e5b2\Thermal Images.pdf"

.\scripts\package_submission.ps1 -InspectionPdf "$inspection" -ThermalPdf "$thermal"
```

## Optional: Use an LLM (better phrasing, same facts)

By default, the generator uses a deterministic heuristic writer (no external calls).
You can optionally enable:

### Option A) OpenAI

Set environment variable:

```powershell
$env:OPENAI_API_KEY="YOUR_KEY"
py .\scripts\generate_ddr.py --inspection "$inspection" --thermal "$thermal" --outdir .\out --llm openai --openai_model gpt-4.1-mini
```

### Option B) Ollama (local)

1) Install Ollama and pull a model, e.g.:

```powershell
ollama pull llama3.1
```

2) Run with:

```powershell
py .\scripts\generate_ddr.py --inspection "$inspection" --thermal "$thermal" --outdir .\out --llm ollama --ollama_model llama3.1
```

## Notes
- The system **does not invent facts**: anything missing becomes **“Not Available”**.
- If it can’t confidently map an extracted image to a specific observation, it will still include the image under a dedicated **Extracted Images** subsection and note the mapping as **Not Available**.

