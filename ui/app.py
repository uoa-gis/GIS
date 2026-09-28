"""FastAPI entry for the flood-mapping UI.

Dates and AOI come from the browser (calendar + Leaflet). Processing constants
stay in processing/defaults.py. The notebook is not executed.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from processing.pipeline import FloodRunRequest, run_flood_mapping

STATIC_DIR = Path(__file__).resolve().parent / "static"
REPO_ROOT = Path(__file__).resolve().parents[1]
LOGO_CANDIDATES = [
    STATIC_DIR / "logo.png",
    REPO_ROOT / "assets" / "logo" / "logo.png",
    REPO_ROOT / "assets" / "logo" / "logo.jpg",
]

app = FastAPI(title="Geographically Informed Speculators", version="0.1.0")

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_run_lock = threading.Lock()


class RunBody(BaseModel):
    """JSON body from the map UI."""

    start_date: str = Field(..., examples=["2023-02-01"])
    end_date: str = Field(..., examples=["2023-02-25"])
    peak_date: str | None = Field(None, examples=["2023-02-15"])
    west: float | None = None
    south: float | None = None
    east: float | None = None
    north: float | None = None
    center_lat: float | None = None
    center_lon: float | None = None
    half_km: float = 5.0


def _worker(job_id: str, body: RunBody) -> None:
    def on_progress(step: int, total: int, label: str) -> None:
        with _jobs_lock:
            _jobs[job_id]["progress"] = {"step": step, "total": total, "label": label}

    with _jobs_lock:
        _jobs[job_id]["progress"] = {"step": 0, "total": 0, "label": "Waiting for previous run"}
    with _run_lock:
        with _jobs_lock:
            _jobs[job_id]["status"] = "running"
        result = run_flood_mapping(
            FloodRunRequest(
                start_date=body.start_date,
                end_date=body.end_date,
                peak_date=body.peak_date,
                west=body.west,
                south=body.south,
                east=body.east,
                north=body.north,
                center_lat=body.center_lat,
                center_lon=body.center_lon,
                half_km=body.half_km,
            ),
            progress=on_progress,
        )
    payload: dict[str, Any] = {
        "ok": result.ok,
        "stats": result.stats,
        "figure_png_base64": result.figure_png_base64,
        "meta": result.meta,
        "error": result.error,
        # Later: POST stats + figure to Anthropic (Claude) and fill this field.
        "report": result.report,
    }
    with _jobs_lock:
        _jobs[job_id]["status"] = "done" if result.ok else "error"
        _jobs[job_id]["result"] = payload


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/static/logo.png")
def team_logo() -> FileResponse:
    for path in LOGO_CANDIDATES:
        if path.is_file():
            return FileResponse(path)
    raise HTTPException(status_code=404, detail="No logo file.")


@app.post("/api/run")
def start_run(body: RunBody) -> dict[str, str]:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "result": None, "progress": None}
    threading.Thread(target=_worker, args=(job_id, body), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/job/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job_id.")
        return {
            "job_id": job_id,
            "status": job["status"],
            "progress": job["progress"],
            "result": job["result"],
        }


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
