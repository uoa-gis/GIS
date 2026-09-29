"""HTML flood report from the comparison figure and area stats.

Uses Google Gemini. The API key is loaded from the repo-root ``.env``
(``GEMINI_KEY`` or ``GOOGLE_API_KEY``). This module is independent of
the notebooks; call it from a notebook cell or from a backend UI.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env")

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

_SYSTEM = """You write an explainable flood / inundation briefing as HTML for a
GEOG761 HA-DR (humanitarian assistance / disaster relief) project.

Team: Geographically Informed Speculators.
Audience: a partner choosing a sea-logistics or beach-landing site after a storm.

Method:
- Event Sentinel-1 GRD IW VV+VH (descending, 10 m).
- Dual-pol water index WI = VV_dB + VH_dB.
- Event water = WI below Otsu(WI) AND WI below a fixed dB cap.
- Subtract permanent water (WorldCover 80, JRC occurrence, DEM nodata sea).
- Keep only low / flat ground (elevation and slope caps).
- Sentinel-2 RGB is context for the figure, not the flood classifier.

Rules:
- Return a complete HTML document only (html, head, body). No Markdown. No code fences.
- You have to convert the input Geo points to the actual location in the report.
- Include a <style> block with simple readable typography (max-width article, tables).
- Put an image placeholder exactly once: <img src="{{FIGURE}}" alt="Flood comparison figure">
- Sections (use <h1>/<h2>): Introduction, Data, Methods, Results, Discussion, Conclusion, References.
- Use only numbers present in the stats/meta JSON. Do not invent areas or dates.
- In Results, explain the two-panel figure (left: Sentinel-1 VV with scale bar and north arrow; right: water index + final flood). Mention the Sentinel-1 acquisition time from meta if present.
  and the stats funnel (event water → minus permanent → terrain → MMU → final).
- State limitations: SAR speckle, no pre/post difference, WorldCover 2021 baseline,
  layover/shadow is a slope-vs-angle proxy, cloud may hide S2 RGB.
- No RAG. No Python. No API keys.
"""


def figure_to_png_bytes(figure: Any) -> bytes:
    """Accept a matplotlib Figure, PNG path, raw bytes, or base64 string."""
    if figure is None:
        raise ValueError("figure is required (matplotlib Figure, PNG path, bytes, or base64).")
    if isinstance(figure, (bytes, bytearray)):
        return bytes(figure)
    if isinstance(figure, str):
        path = Path(figure)
        if path.is_file():
            return path.read_bytes()
        return base64.b64decode(figure)
    if isinstance(figure, Path):
        return figure.read_bytes()
    if hasattr(figure, "save") and not hasattr(figure, "savefig"):
        buf = io.BytesIO()
        figure.save(buf, format="PNG")
        return buf.getvalue()
    if hasattr(figure, "savefig"):
        buf = io.BytesIO()
        figure.savefig(buf, format="png", dpi=120, bbox_inches="tight")
        return buf.getvalue()
    raise TypeError(f"Unsupported figure type: {type(figure)!r}")


def _client():
    from google import genai

    key = os.environ.get("GEMINI_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError(
            "Missing API key. Set GEMINI_KEY (or GOOGLE_API_KEY) in the repo-root .env."
        )
    return genai.Client(api_key=key)


def _strip_fences(text: str) -> str:
    """Drop ```html / ``` wrappers if the model adds them."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.endswith("```"):
            stripped = stripped[: stripped.rfind("```")].rstrip()
    return stripped.strip()


def _embed_figure(html: str, png: bytes) -> str:
    """Replace {{FIGURE}} or insert a data-URI image of the comparison figure."""
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    img = f'<img src="{uri}" alt="Flood comparison figure" style="max-width:100%;height:auto;">'
    if "{{FIGURE}}" in html:
        return html.replace("{{FIGURE}}", uri)
    match = re.search(r"<body[^>]*>", html, flags=re.IGNORECASE)
    if match:
        i = match.end()
        return html[:i] + "\n" + img + html[i:]
    return img + "\n" + html


def generate_flood_report(
    stats: dict[str, Any],
    figure: Any,
    meta: dict[str, Any] | None = None,
    *,
    model: str | None = None,
    max_tokens: int = 8192,
) -> str:
    """Ask Gemini to explain the flood figure using the area statistics.

    Returns a complete HTML document (figure embedded as a data URI).
    Does not print or log the API key.
    """
    from google.genai import types

    png = figure_to_png_bytes(figure)
    payload = {"stats": stats, "meta": meta or {}}
    user_text = (
        "Write a complete HTML report that explains this figure using these "
        "statistics and metadata. Output HTML only.\n\n"
        f"```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    )
    client = _client()
    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM,
        max_output_tokens=max_tokens,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    response = client.models.generate_content(
        model=model or DEFAULT_MODEL,
        contents=[
            types.Part.from_bytes(data=png, mime_type="image/png"),
            user_text,
        ],
        config=config,
    )
    html = _strip_fences(response.text or "")
    return _embed_figure(html, png)


def html_as_iframe(html: str, *, height: str = "85vh") -> str:
    """Wrap a full HTML document so its CSS cannot restyle the notebook.

    Jupyter / VS Code injects ``display(HTML(...))`` into the page. Unscoped
    rules such as ``body``, ``h1``, ``p`` then hide or restyle markdown cells.
    An iframe with ``srcdoc`` keeps the report isolated.
    """
    srcdoc = html.replace("&", "&amp;").replace('"', "&quot;")
    return (
        f'<iframe srcdoc="{srcdoc}" '
        f'style="width:100%;height:{height};border:1px solid #ccc;background:#fff;" '
        'sandbox="allow-same-origin"></iframe>'
    )

