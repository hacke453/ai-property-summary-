from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import fitz  # PyMuPDF
import markdown as md
import requests
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, select_autoescape


LLMMode = Literal["none", "openai", "ollama"]


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _slug(s: str, max_len: int = 64) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").strip()).strip("-").lower()
    if not s:
        return "item"
    return s[:max_len].strip("-")


def _safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def extract_pdf_text_by_page(pdf_path: Path) -> list[str]:
    doc = fitz.open(pdf_path)
    try:
        pages: list[str] = []
        for page in doc:
            pages.append(page.get_text("text") or "")
        return pages
    finally:
        doc.close()


def extract_pdf_text(pdf_path: Path) -> str:
    pages = extract_pdf_text_by_page(pdf_path)
    parts = [p for p in pages if (p or "").strip()]
    return "\n".join(parts)


@dataclass
class ExtractedImage:
    source: str  # "inspection" | "thermal"
    page: int
    filename: str
    path: str
    width: int | None = None
    height: int | None = None


def extract_pdf_images(pdf_path: Path, out_assets_dir: Path, source_tag: str) -> list[ExtractedImage]:
    _safe_mkdir(out_assets_dir)
    doc = fitz.open(pdf_path)
    extracted: list[ExtractedImage] = []
    try:
        for page_index in range(len(doc)):
            page = doc[page_index]
            img_list = page.get_images(full=True) or []
            for img_i, img in enumerate(img_list, start=1):
                xref = img[0]
                try:
                    base = doc.extract_image(xref)
                except Exception:
                    continue

                ext = (base.get("ext") or "bin").lower()
                data = base.get("image")
                if not data:
                    continue

                # Keep original ext if common, otherwise default to png.
                if ext not in {"png", "jpg", "jpeg", "webp"}:
                    ext = "png"
                    # If unknown, just write raw bytes; browsers may not display.
                    # Still useful for submission artifacts.
                fname = f"{source_tag}_p{page_index+1:03d}_img{img_i:02d}.{ext}"
                fpath = out_assets_dir / fname
                with open(fpath, "wb") as f:
                    f.write(data)

                extracted.append(
                    ExtractedImage(
                        source=source_tag,
                        page=page_index + 1,
                        filename=fname,
                        path=str(fpath.as_posix()),
                        width=base.get("width"),
                        height=base.get("height"),
                    )
                )
        return extracted
    finally:
        doc.close()


@dataclass
class ImpactedArea:
    area_id: str
    page: int | None
    negative: str | None
    positive: str | None
    raw_block: str | None = None


def _find_first_page_index_containing(pages: list[str], needle: str) -> int | None:
    if not needle:
        return None
    n = needle.lower()
    for i, p in enumerate(pages):
        if n in (p or "").lower():
            return i + 1  # 1-based
    return None


def parse_impacted_areas(inspection_text: str, inspection_pages: list[str] | None = None) -> list[ImpactedArea]:
    # Extract blocks that look like:
    # Impacted Area X
    # Negative side Description ...
    # Positive side Description ...
    t = inspection_text
    # Normalize the common OCR quirks ("Impacted Area" vs "Impacted area")
    pattern = re.compile(
        r"(Impacted\s+Area\s+\d+)([\s\S]*?)(?=(Impacted\s+Area\s+\d+)|\Z)",
        re.IGNORECASE,
    )
    areas: list[ImpactedArea] = []
    for m in pattern.finditer(t):
        header = _norm_ws(m.group(1))
        block = m.group(2) or ""
        neg = None
        pos = None

        neg_m = re.search(r"Negative\s+side\s+Description\s*(.*)", block, re.IGNORECASE)
        if neg_m:
            neg_line = neg_m.group(1).strip()
            neg = _norm_ws(neg_line) if neg_line else None

        pos_m = re.search(r"Positive\s+side\s+Description\s*(.*)", block, re.IGNORECASE)
        if pos_m:
            pos_line = pos_m.group(1).strip()
            pos = _norm_ws(pos_line) if pos_line else None

        area_id = _slug(header)
        page_no = None
        if inspection_pages is not None:
            page_no = _find_first_page_index_containing(inspection_pages, header)
        areas.append(
            ImpactedArea(
                area_id=area_id,
                page=page_no,
                negative=neg,
                positive=pos,
                raw_block=_norm_ws(block)[:2000] or None,
            )
        )

    # De-duplicate by (negative, positive)
    seen: set[tuple[str | None, str | None]] = set()
    deduped: list[ImpactedArea] = []
    for a in areas:
        key = (_norm_ws(a.negative or "").lower() or None, _norm_ws(a.positive or "").lower() or None)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(a)
    return deduped


@dataclass
class InspectionSummaryPoint:
    point_no: str
    impacted: str
    exposed: str | None = None


def parse_summary_table(inspection_text: str) -> list[InspectionSummaryPoint]:
    t = inspection_text
    # Lines often look like:
    # 1 Observed dampness ... 1.1 Observed gaps ...
    points: list[InspectionSummaryPoint] = []
    # We try a conservative pattern: start of line digit+ space "Observed ..."
    for line in t.splitlines():
        line_n = _norm_ws(line)
        m = re.match(r"^(\d+)\s+(Observed\b.*)$", line_n, re.IGNORECASE)
        if not m:
            continue
        point_no = m.group(1)
        impacted = m.group(2)
        points.append(InspectionSummaryPoint(point_no=point_no, impacted=impacted))

    # Try pairing with exposed (+ve side) points like "1.1 Observed ..."
    exposed_map: dict[str, str] = {}
    for line in t.splitlines():
        line_n = _norm_ws(line)
        m = re.match(r"^(\d+\.\d+)\s+(Observed\b.*)$", line_n, re.IGNORECASE)
        if not m:
            continue
        exposed_map[m.group(1)] = m.group(2)

    # Attach the first matching exposed point per impacted point, if it exists.
    out: list[InspectionSummaryPoint] = []
    for p in points:
        # common mapping: 1 -> 1.1, 2 -> 2.1 ...
        exp = exposed_map.get(f"{p.point_no}.1")
        out.append(InspectionSummaryPoint(point_no=p.point_no, impacted=p.impacted, exposed=exp))

    # De-dupe impacted strings
    seen_impacted: set[str] = set()
    deduped: list[InspectionSummaryPoint] = []
    for p in out:
        k = _norm_ws(p.impacted).lower()
        if not k or k in seen_impacted:
            continue
        seen_impacted.add(k)
        deduped.append(p)
    return deduped


@dataclass
class ThermalFinding:
    seq: int
    thermal_image_filename: str | None
    hotspot_c: float | None
    coldspot_c: float | None
    date: str | None
    emissivity: float | None
    reflected_temp_c: float | None


def _parse_float(s: str) -> float | None:
    try:
        return float(s)
    except Exception:
        return None


def parse_thermal_findings(thermal_text: str) -> list[ThermalFinding]:
    t = thermal_text
    # OCR format in your sample:
    # Hotspot : 28.8 °C
    # Coldspot : 23.4 °C
    # Emissivity : 0.94
    # Reflected temperature : 23 °C
    # Thermal image : RB02380X.JPG
    blocks = re.split(r"\n\s*(?=\d+\s*$)", t, flags=re.MULTILINE)
    findings: list[ThermalFinding] = []
    seq = 0
    for b in blocks:
        b2 = b.strip()
        if "Hotspot" not in b2 and "Thermal image" not in b2:
            continue
        seq += 1
        hotspot = None
        coldspot = None
        img = None
        emiss = None
        refl = None
        date = None

        m = re.search(r"Hotspot\s*:\s*([0-9.]+)\s*°?C", b2, re.IGNORECASE)
        if m:
            hotspot = _parse_float(m.group(1))
        m = re.search(r"Coldspot\s*:\s*([0-9.]+)\s*°?C", b2, re.IGNORECASE)
        if m:
            coldspot = _parse_float(m.group(1))
        m = re.search(r"Thermal\s+image\s*:\s*([A-Za-z0-9_.-]+)", b2, re.IGNORECASE)
        if m:
            img = m.group(1).strip()
        m = re.search(r"Emissivity\s*:\s*([0-9.]+)", b2, re.IGNORECASE)
        if m:
            emiss = _parse_float(m.group(1))
        m = re.search(r"Reflected\s+temperature\s*:\s*([0-9.]+)\s*°?C", b2, re.IGNORECASE)
        if m:
            refl = _parse_float(m.group(1))
        m = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4})", b2)
        if m:
            date = m.group(1)

        findings.append(
            ThermalFinding(
                seq=seq,
                thermal_image_filename=img,
                hotspot_c=hotspot,
                coldspot_c=coldspot,
                date=date,
                emissivity=emiss,
                reflected_temp_c=refl,
            )
        )
    return findings


def severity_for_text(observation: str) -> tuple[str, str]:
    """
    Returns (severity_label, reasoning) based only on observed words.
    """
    o = (observation or "").lower()
    if any(k in o for k in ["live leakage", "leakage", "outlet leakage"]):
        return ("High", "Leakage is explicitly observed, which can cause rapid damage if not addressed.")
    if any(k in o for k in ["seepage", "efflorescence", "spalling"]):
        return ("Medium", "Seepage/efflorescence/spalling indicate ongoing moisture movement through finishes.")
    if any(k in o for k in ["dampness", "damp", "mild"]):
        return ("Medium", "Dampness indicates moisture presence; it can worsen if the source continues.")
    if "crack" in o:
        return ("Medium", "Cracks can enable water ingress and may propagate; needs monitoring and repair.")
    return ("Low", "No strong severity cues found in the extracted text.")


def root_cause_hypotheses(observations: list[str]) -> list[str]:
    """
    Conservative cause suggestions based strictly on observed keywords.
    """
    all_text = " \n ".join([o for o in observations if o]).lower()
    causes: list[str] = []
    if "tile joint" in all_text or "gaps" in all_text:
        causes.append("Water ingress through gaps in tile joints (as observed) leading to moisture movement into adjacent finishes.")
    if "concealed plumbing" in all_text or "plumbing" in all_text:
        causes.append("Possible concealed plumbing contribution is indicated in the checklist responses (where marked as 'Yes').")
    if "external wall" in all_text and "crack" in all_text:
        causes.append("Water ingress through external wall cracks (as observed) leading to interior dampness.")
    if "terrace" in all_text and ("crack" in all_text or "hollow" in all_text or "vegetation" in all_text):
        causes.append("Terrace surface distress (cracks/hollowness/vegetation as observed) allowing water ingress into the slab/finishes below.")
    return causes or ["Not Available"]


def recommended_actions(observations: list[str]) -> list[str]:
    """
    Action list tied to observed issue types. These are recommendations, not facts.
    """
    all_text = " \n ".join([o for o in observations if o]).lower()
    actions: list[str] = []
    if "tile joint" in all_text or "gaps" in all_text:
        actions.append("Re-grout tile joints where gaps/blackish dirt are observed; seal corners and around Nahani traps if applicable.")
    if "plumbing" in all_text or "outlet" in all_text:
        actions.append("Inspect and repair plumbing joints/outlets where leakage is suspected or observed; conduct a controlled water test after repairs.")
    if "external wall" in all_text and "crack" in all_text:
        actions.append("Repair and seal external wall cracks, then apply a suitable exterior waterproof coating system to reduce water ingress.")
    if "terrace" in all_text and ("crack" in all_text or "hollow" in all_text or "vegetation" in all_text):
        actions.append("Remove vegetation and repair terrace cracks/hollow areas; restore slope and waterproofing system as required.")
    if "plaster" in all_text or "paint" in all_text or "spalling" in all_text:
        actions.append("After stopping moisture ingress, remove damaged paint/plaster and reinstate finishes with appropriate primers and waterproof additives.")
    return actions or ["Not Available"]


def render_report_markdown(payload: dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(str((Path(__file__).parent / "templates").resolve())),
        autoescape=select_autoescape(enabled_extensions=("html",)),
    )
    tpl = env.get_template("ddr.md.j2")
    return tpl.render(**payload)


def markdown_to_html(markdown_text: str) -> str:
    return md.markdown(
        markdown_text,
        extensions=["tables", "fenced_code", "toc"],
        output_format="html5",
    )


def call_ollama(model: str, prompt: str) -> str:
    # Assumes local ollama default endpoint.
    resp = requests.post(
        "http://localhost:11434/api/generate",
        json={"model": model, "prompt": prompt, "stream": False},
        timeout=180,
    )
    resp.raise_for_status()
    data = resp.json()
    return (data.get("response") or "").strip()


def call_openai(model: str, prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI()
    r = client.responses.create(
        model=model,
        input=prompt,
    )
    # best-effort: join all output text
    out_parts: list[str] = []
    for item in r.output:
        if item.type == "message":
            for c in item.content:
                if c.type == "output_text":
                    out_parts.append(c.text)
    return _norm_ws("\n".join(out_parts))


def maybe_llm_rewrite(mode: LLMMode, model: str, draft_markdown: str) -> str:
    if mode == "none":
        return draft_markdown

    # Strict: keep structure, do not add new facts.
    prompt = f"""
You are rewriting a client-ready inspection DDR report.
Rules:
- DO NOT add any new facts, numbers, areas, or claims.
- Keep the same headings and section order.
- If the draft says "Not Available", keep it as-is.
- If a conflict is mentioned, keep the conflict.
- Improve clarity, grammar, and client-friendly language only.

Return ONLY the rewritten markdown.

--- DRAFT MARKDOWN START ---
{draft_markdown}
--- DRAFT MARKDOWN END ---
""".strip()

    if mode == "ollama":
        return call_ollama(model=model, prompt=prompt)
    if mode == "openai":
        return call_openai(model=model, prompt=prompt)
    return draft_markdown


def main() -> int:
    load_dotenv()

    ap = argparse.ArgumentParser(description="Generate a Main DDR from Inspection + Thermal PDFs.")
    ap.add_argument("--inspection", required=True, help="Path to Inspection PDF")
    ap.add_argument("--thermal", required=True, help="Path to Thermal PDF")
    ap.add_argument("--outdir", required=True, help="Output directory")
    ap.add_argument("--llm", choices=["none", "openai", "ollama"], default="none", help="Optional LLM rewrite mode")
    ap.add_argument("--openai_model", default="gpt-4.1-mini", help="OpenAI model to use when --llm openai")
    ap.add_argument("--ollama_model", default="llama3.1", help="Ollama model to use when --llm ollama")
    args = ap.parse_args()

    inspection_pdf = Path(args.inspection).expanduser().resolve()
    thermal_pdf = Path(args.thermal).expanduser().resolve()
    outdir = Path(args.outdir).expanduser().resolve()

    if not inspection_pdf.exists():
        raise SystemExit(f"Inspection PDF not found: {inspection_pdf}")
    if not thermal_pdf.exists():
        raise SystemExit(f"Thermal PDF not found: {thermal_pdf}")

    # Prepare output dirs
    if outdir.exists():
        # keep it clean for reproducibility
        shutil.rmtree(outdir)
    _safe_mkdir(outdir)
    assets_dir = outdir / "assets"
    extracted_dir = outdir / "extracted"
    _safe_mkdir(assets_dir)
    _safe_mkdir(extracted_dir)

    # Extract
    inspection_pages = extract_pdf_text_by_page(inspection_pdf)
    thermal_pages = extract_pdf_text_by_page(thermal_pdf)
    inspection_text = "\n".join([p for p in inspection_pages if (p or "").strip()])
    thermal_text = "\n".join([p for p in thermal_pages if (p or "").strip()])

    inspection_images = extract_pdf_images(inspection_pdf, assets_dir, "inspection")
    thermal_images = extract_pdf_images(thermal_pdf, assets_dir, "thermal")

    impacted_areas = parse_impacted_areas(inspection_text, inspection_pages=inspection_pages)
    summary_points = parse_summary_table(inspection_text)
    thermal_findings = parse_thermal_findings(thermal_text)

    # Build observation list used by multiple sections
    obs: list[str] = []
    for p in summary_points:
        obs.append(p.impacted)
        if p.exposed:
            obs.append(p.exposed)
    for a in impacted_areas:
        if a.negative:
            obs.append(a.negative)
        if a.positive:
            obs.append(a.positive)

    obs = [_norm_ws(o) for o in obs if _norm_ws(o)]

    # Dedupe preserving order
    seen_obs: set[str] = set()
    obs_deduped: list[str] = []
    for o in obs:
        k = o.lower()
        if k in seen_obs:
            continue
        seen_obs.add(k)
        obs_deduped.append(o)

    # Severity assessment: take the maximum severity across observations
    severity_rank = {"Low": 1, "Medium": 2, "High": 3}
    sev_label = "Low"
    sev_reasons: list[str] = []
    for o in obs_deduped:
        l, r = severity_for_text(o)
        if severity_rank[l] > severity_rank[sev_label]:
            sev_label = l
        sev_reasons.append(f"- {o} → {l}: {r}")

    # Area-wise grouping (best-effort)
    images_by_page: dict[int, list[ExtractedImage]] = {}
    for im in inspection_images:
        images_by_page.setdefault(im.page, []).append(im)

    area_sections: list[dict[str, Any]] = []
    used_image_filenames: set[str] = set()
    max_images_per_area = 4
    for a in impacted_areas:
        items: list[str] = []
        if a.negative:
            items.append(a.negative)
        if a.positive:
            items.append(a.positive)
        if not items:
            items = ["Not Available"]

        mapped_images: list[dict[str, Any]] = []
        omitted_images_count = 0
        if a.page is not None:
            for im in images_by_page.get(a.page, []):
                if im.filename in used_image_filenames:
                    continue
                if len(mapped_images) >= max_images_per_area:
                    omitted_images_count += 1
                    continue
                used_image_filenames.add(im.filename)
                mapped_images.append({"filename": im.filename, "page": im.page, "rel_path": f"assets/{im.filename}"})

            # Count remaining unused images on same page (for transparency)
            if omitted_images_count == 0:
                remaining_unused = [
                    im for im in images_by_page.get(a.page, []) if im.filename not in used_image_filenames
                ]
                if len(mapped_images) >= max_images_per_area and remaining_unused:
                    omitted_images_count = len(remaining_unused)
        area_sections.append(
            {
                "area_id": a.area_id,
                "title": a.area_id.replace("-", " ").title(),
                "observations": items,
                "images": mapped_images,
                "image_mapping_note": "Not Available" if not mapped_images else None,
                "images_omitted_note": (
                    f"{omitted_images_count} additional image(s) from the same page were extracted but not shown here."
                    if omitted_images_count
                    else None
                ),
            }
        )

    if not area_sections:
        area_sections = [
            {
                "area_id": "not-available",
                "title": "Not Available",
                "observations": ["Not Available"],
                "images": [],
                "image_mapping_note": "Not Available",
            }
        ]

    payload: dict[str, Any] = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "inputs": {
            "inspection_pdf": str(inspection_pdf),
            "thermal_pdf": str(thermal_pdf),
        },
        "property_issue_summary": obs_deduped[:12] if obs_deduped else ["Not Available"],
        "area_wise": area_sections,
        "probable_root_cause": root_cause_hypotheses(obs_deduped),
        "severity": {
            "label": sev_label,
            "reasoning": sev_reasons[:20] if sev_reasons else ["Not Available"],
        },
        "recommended_actions": recommended_actions(obs_deduped),
        "additional_notes": [
            "This DDR is generated from the provided PDFs using text/image extraction. It is not a code-compliance or destructive testing report.",
            "If any observation requires structural verification, consult a qualified engineer/contractor.",
        ],
        "missing_or_unclear": [
            "Mapping between specific 'Photo X' references and embedded images: Not Available in extracted text.",
            "Exact location mapping between thermal frames and rooms/areas: Not Available in thermal text.",
        ],
        "extracted": {
            "inspection": {
                "impacted_areas": [asdict(x) for x in impacted_areas],
                "summary_points": [asdict(x) for x in summary_points],
                "images": [asdict(x) for x in inspection_images],
            },
            "thermal": {
                "findings": [asdict(x) for x in thermal_findings],
                "images": [asdict(x) for x in thermal_images],
            },
        },
    }

    # Write intermediate JSON
    (extracted_dir / "inspection.json").write_text(json.dumps(payload["extracted"]["inspection"], indent=2), encoding="utf-8")
    (extracted_dir / "thermal.json").write_text(json.dumps(payload["extracted"]["thermal"], indent=2), encoding="utf-8")

    # Draft markdown
    draft_md = render_report_markdown(payload)

    # Optional LLM rewrite
    llm_mode: LLMMode = args.llm
    if llm_mode == "openai":
        final_md = maybe_llm_rewrite("openai", args.openai_model, draft_md)
    elif llm_mode == "ollama":
        final_md = maybe_llm_rewrite("ollama", args.ollama_model, draft_md)
    else:
        final_md = draft_md

    (outdir / "ddr.md").write_text(final_md, encoding="utf-8")
    html_body = markdown_to_html(final_md)
    html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>Main DDR</title>
    <style>
      body {{ font-family: Arial, sans-serif; line-height: 1.5; max-width: 900px; margin: 40px auto; padding: 0 20px; }}
      h1, h2, h3 {{ margin-top: 1.2em; }}
      code, pre {{ background: #f6f8fa; }}
      img {{ max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 6px; }}
      .meta {{ color: #444; font-size: 0.95em; }}
    </style>
  </head>
  <body>
    {html_body}
  </body>
</html>
"""
    (outdir / "ddr.html").write_text(html, encoding="utf-8")

    print(f"Generated: {outdir / 'ddr.md'}")
    print(f"Generated: {outdir / 'ddr.html'}")
    print(f"Extracted assets: {assets_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

