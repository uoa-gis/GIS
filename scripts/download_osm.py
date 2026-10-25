"""Download OpenStreetMap roads and buildings for NZ and the Pacific (run once).

Source: Geofabrik daily country extracts (GeoPackage, "free" layers),
https://download.geofabrik.de/australia-oceania.html. Data © OpenStreetMap
contributors, ODbL 1.0.

For each country this downloads ``<slug>-latest-free.gpkg.zip`` to
``data/raw/osm/`` (gitignored, reused on later runs) and writes:

- ``data/osm-roads/<slug>.gpkg``     drivable road centrelines (motorway ... residential)
- ``data/osm-buildings/<slug>.gpkg`` building polygons (Pacific only by default;
  NZ uses LINZ building outlines)

``processing/roads.py`` and ``processing/buildings.py`` read these files for the
AOI before falling back to the live Overpass server.

Usage (from the repo root):

    uv run python scripts/download_osm.py                 # Pacific roads+buildings, NZ roads
    uv run python scripts/download_osm.py fiji samoa      # just these countries
    uv run python scripts/download_osm.py --no-nz         # Pacific only
    uv run python scripts/download_osm.py --nz-buildings  # also OSM buildings for NZ
    uv run python scripts/download_osm.py --refresh       # re-download newer extracts

Re-run with --refresh to pick up recent OSM edits (e.g. after a HOT mapping campaign).
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from datetime import date
from pathlib import Path
from urllib.request import urlopen

import geopandas as gpd
import pandas as pd
import pyogrio

REPO = Path(__file__).resolve().parents[1]
RAW_DIR = REPO / "data" / "raw" / "osm"
ROADS_DIR = REPO / "data" / "osm-roads"
BUILDINGS_DIR = REPO / "data" / "osm-buildings"
BASE = "https://download.geofabrik.de/australia-oceania/{slug}-latest-free.gpkg.zip"
NZ = "new-zealand"

# Geofabrik slugs (Australia has no GeoPackage extract).
PACIFIC = [
    "american-oceania",
    "cook-islands",
    "fiji",
    "kiribati",
    "marshall-islands",
    "micronesia",
    "nauru",
    "new-caledonia",
    "niue",
    "palau",
    "papua-new-guinea",
    "pitcairn-islands",
    "polynesie-francaise",
    "samoa",
    "solomon-islands",
    "tokelau",
    "tonga",
    "tuvalu",
    "vanuatu",
    "wallis-et-futuna",
]

# Same classes as osmnx network_type="drive" (no service roads, tracks or paths).
DRIVE_CLASSES = (
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
    "residential", "living_street", "road", "busway",
)


def _download(url: str, dest: Path, refresh: bool) -> None:
    if dest.exists() and not refresh:
        print(f"  using cached {dest.name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urlopen(url, timeout=120) as resp, open(tmp, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done / 1e6:7.1f} / {total / 1e6:.1f} MB", end="", flush=True)
    print()
    tmp.replace(dest)


def _layers(gpkgs: list[Path], word: str) -> list[tuple[Path, str]]:
    out = []
    for gpkg in gpkgs:
        for layer_name, _geom_type in pyogrio.list_layers(gpkg):
            if word in str(layer_name).lower():
                out.append((gpkg, str(layer_name)))
    return out


def _yes_no(val) -> str:
    return "yes" if str(val).strip().upper() in ("T", "TRUE", "YES", "1") else "no"


def _write_roads(slug: str, gpkgs: list[Path]) -> None:
    layers = _layers(gpkgs, "roads")
    if not layers:
        raise RuntimeError(f"{slug}: no roads layer in {[g.name for g in gpkgs]}")
    where = "fclass IN (" + ", ".join(f"'{c}'" for c in DRIVE_CLASSES) + ")"
    parts = []
    for gpkg, layer in layers:
        try:
            parts.append(gpd.read_file(gpkg, layer=layer, where=where))
        except Exception:  # noqa: BLE001 — column name differs; filter after reading
            g = gpd.read_file(gpkg, layer=layer)
            if "fclass" in g.columns:
                g = g[g["fclass"].isin(DRIVE_CLASSES)]
            parts.append(g)
    gdf = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs).to_crs("EPSG:4326")
    gdf = gdf[gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])]
    gdf = gdf.rename(columns={"fclass": "highway"})
    for col in ("bridge", "tunnel"):
        gdf[col] = gdf[col].map(_yes_no) if col in gdf.columns else "no"
    if "oneway" not in gdf.columns:
        gdf["oneway"] = "B"
    keep = [c for c in ("osm_id", "highway", "name", "ref", "oneway", "bridge", "tunnel") if c in gdf.columns]
    gdf = gdf[keep + ["geometry"]].reset_index(drop=True)
    ROADS_DIR.mkdir(parents=True, exist_ok=True)
    out = ROADS_DIR / f"{slug}.gpkg"
    gdf.to_file(out, layer="roads", driver="GPKG")
    print(f"  roads: {len(gdf):,} drivable segments -> {out.relative_to(REPO)}")


def _write_buildings(slug: str, gpkgs: list[Path]) -> None:
    layers = _layers(gpkgs, "building")
    if not layers:
        raise RuntimeError(f"{slug}: no building layer in {[g.name for g in gpkgs]}")
    parts = [gpd.read_file(gpkg, layer=layer) for gpkg, layer in layers]
    gdf = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs).to_crs("EPSG:4326")
    gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    gdf = gdf.rename(columns={k: v for k, v in {"type": "building", "fclass": "osm_class"}.items() if k in gdf.columns})
    keep = [c for c in ("osm_id", "building", "osm_class", "name") if c in gdf.columns]
    gdf = gdf[keep + ["geometry"]].reset_index(drop=True)
    BUILDINGS_DIR.mkdir(parents=True, exist_ok=True)
    out = BUILDINGS_DIR / f"{slug}.gpkg"
    gdf.to_file(out, layer="buildings", driver="GPKG")
    print(f"  buildings: {len(gdf):,} polygons -> {out.relative_to(REPO)}")


def process(slug: str, roads: bool, buildings: bool, refresh: bool) -> None:
    print(f"{slug}:")
    zpath = RAW_DIR / f"{slug}-latest-free.gpkg.zip"
    _download(BASE.format(slug=slug), zpath, refresh)
    unpacked = RAW_DIR / slug
    if unpacked.exists():
        shutil.rmtree(unpacked)
    with zipfile.ZipFile(zpath) as zf:
        zf.extractall(unpacked)
    try:
        gpkgs = sorted(unpacked.rglob("*.gpkg"))
        if not gpkgs:
            raise RuntimeError(f"no .gpkg inside {zpath.name}")
        if roads:
            _write_roads(slug, gpkgs)
        if buildings:
            _write_buildings(slug, gpkgs)
    finally:
        shutil.rmtree(unpacked, ignore_errors=True)  # unzipped copy is large; keep only the zip


def main(argv: list[str]) -> int:
    flags = {a for a in argv if a.startswith("--")}
    named = [a for a in argv if not a.startswith("--")]
    refresh = "--refresh" in flags
    nz_buildings = "--nz-buildings" in flags
    slugs = named or (PACIFIC + ([] if "--no-nz" in flags else [NZ]))

    failed = []
    for slug in slugs:
        try:
            is_nz = slug == NZ
            process(slug, roads=True, buildings=(not is_nz) or nz_buildings, refresh=refresh)
        except Exception as exc:  # noqa: BLE001 — keep going with the other countries
            print(f"{slug}: FAILED ({type(exc).__name__}: {exc})")
            failed.append(slug)

    note = (
        "OpenStreetMap data from Geofabrik extracts "
        f"(https://download.geofabrik.de/australia-oceania.html), downloaded {date.today().isoformat()}.\n"
        "Data © OpenStreetMap contributors, ODbL 1.0.\n"
    )
    for folder in (ROADS_DIR, BUILDINGS_DIR):
        if folder.exists():
            (folder / "SOURCE.txt").write_text(note, encoding="utf-8")
    print(f"Done. {len(slugs) - len(failed)} of {len(slugs)} ok" + (f"; failed: {', '.join(failed)}" if failed else "."))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
