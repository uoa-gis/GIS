"""Pick one Sentinel-1 orbit track for the event and historical stacks.

ΔWI (historical WI − event WI) is only meaningful when both images share one
viewing geometry: the same relative orbit and pass direction. A track is chosen
in two steps:

1. Keep tracks whose event-window scenes cover at least ``min_coverage`` of the AOI.
   A track that only clips the AOI edge gives an almost empty map.
2. Among those, take the track whose scene is closest in time to the flood peak.

If no single track covers the AOI (large AOIs straddling a swath edge), the
caller mixes all tracks, as the pipeline did before, and the message says so.
Used by notebooks 01 and 04 and by processing/pipeline.py.
"""

from __future__ import annotations

from typing import Any

import ee

MIN_TRACK_COVERAGE = 0.95


def choose_track(
    col: ee.ImageCollection,
    aoi: ee.Geometry,
    peak_date: str,
    match: bool = True,
    min_coverage: float = MIN_TRACK_COVERAGE,
) -> dict[str, Any]:
    """Return the chosen track and the scene nearest the peak on it.

    Keys: ``matched`` (bool), ``rel_orbit``, ``orbit_pass``, ``coverage``
    (fraction of AOI, chosen track), ``closest_id``, ``peak_diff_days``,
    ``tracks`` (per-track summary) and ``message`` (one line to print).
    """
    peak = ee.Date(peak_date)
    rows = (
        col.map(lambda img: img.set("peak_diff_days", ee.Number(img.date().difference(peak, "day")).abs()))
        .reduceColumns(
            ee.Reducer.toList(4),
            ["relativeOrbitNumber_start", "orbitProperties_pass", "peak_diff_days", "system:index"],
        )
        .get("list")
        .getInfo()
    )
    if not rows:
        raise ValueError("No Sentinel-1 scenes in the event window for this AOI.")

    tracks: dict[tuple[int, str], dict[str, Any]] = {}
    for orbit, orbit_pass, diff, idx in rows:
        key = (int(orbit), str(orbit_pass))
        t = tracks.setdefault(key, {"peak_diff_days": float("inf"), "closest_id": None, "n_scenes": 0})
        t["n_scenes"] += 1
        if diff < t["peak_diff_days"]:
            t["peak_diff_days"] = float(diff)
            t["closest_id"] = idx

    # Coverage of the AOI by each track's union of scene footprints (one getInfo).
    aoi_area = aoi.area(maxError=50)
    cover = {}
    for orbit, orbit_pass in tracks:
        sub = col.filter(ee.Filter.eq("relativeOrbitNumber_start", orbit)).filter(
            ee.Filter.eq("orbitProperties_pass", orbit_pass)
        )
        footprint = sub.geometry().dissolve(maxError=50)
        cover[f"{orbit}_{orbit_pass}"] = footprint.intersection(aoi, maxError=50).area(maxError=50).divide(aoi_area)
    cover = ee.Dictionary(cover).getInfo()
    for (orbit, orbit_pass), t in tracks.items():
        t["coverage"] = float(cover[f"{orbit}_{orbit_pass}"])

    overall = min(tracks.items(), key=lambda kv: kv[1]["peak_diff_days"])
    full = {k: t for k, t in tracks.items() if t["coverage"] >= min_coverage}
    summary = ", ".join(
        f"{o} {p[:4]} cov {t['coverage']:.0%} Δ{t['peak_diff_days']:.2f} d" for (o, p), t in sorted(tracks.items())
    )

    if match and full:
        (orbit, orbit_pass), t = min(full.items(), key=lambda kv: kv[1]["peak_diff_days"])
        msg = (
            f"Track: relative orbit {orbit} {orbit_pass}, covers {t['coverage']:.0%} of AOI "
            f"(event and historical stacks restricted to it). Candidates: {summary}"
        )
        return {
            "matched": True,
            "rel_orbit": orbit,
            "orbit_pass": orbit_pass,
            "coverage": t["coverage"],
            "closest_id": t["closest_id"],
            "peak_diff_days": t["peak_diff_days"],
            "tracks": summary,
            "message": msg,
        }

    (orbit, orbit_pass), t = overall
    reason = "MATCH_ORBIT is off" if not match else f"no single track covers ≥{min_coverage:.0%} of the AOI"
    msg = f"WARNING: mixing orbit tracks ({reason}); ΔWI mixes viewing geometries. Candidates: {summary}"
    return {
        "matched": False,
        "rel_orbit": None,
        "orbit_pass": None,
        "coverage": max(tt["coverage"] for tt in tracks.values()),
        "closest_id": t["closest_id"],
        "peak_diff_days": t["peak_diff_days"],
        "tracks": summary,
        "message": msg,
    }


def filter_track(col: ee.ImageCollection, track: dict[str, Any]) -> ee.ImageCollection:
    """Restrict a collection to the chosen track (no-op when tracks are mixed)."""
    if not track["matched"]:
        return col
    return col.filter(ee.Filter.eq("relativeOrbitNumber_start", track["rel_orbit"])).filter(
        ee.Filter.eq("orbitProperties_pass", track["orbit_pass"])
    )
