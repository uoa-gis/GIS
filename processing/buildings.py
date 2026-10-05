"""LINZ NZ Building Outlines ∩ flood polygons."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd
from shapely.geometry import box, shape

NZTM = "EPSG:2193"
WGS84 = "EPSG:4326"

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_PATHS = (
    "data/nz-building/nz-building-outlines.shp",
    "data/nz-bulding/nz-building-outlines.shp",
)


def resolve_building_path(configured: str | None = None) -> Path | None:
    """Return the first existing shapefile / GeoPackage path."""
    candidates: list[Path] = []
    if configured:
        p = Path(configured)
        candidates.append(p if p.is_absolute() else _REPO / p)
    candidates.extend(_REPO / rel for rel in _DEFAULT_PATHS)
    for path in candidates:
        if path.exists():
            return path
    return None


def ee_fc_to_gdf(fc) -> gpd.GeoDataFrame:
    """Convert a small Earth Engine FeatureCollection via getInfo()."""
    payload = fc.getInfo() or {}
    feats = payload.get("features") or []
    if not feats:
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)
    rows = []
    geoms = []
    for feat in feats:
        props = dict(feat.get("properties") or {})
        geom = feat.get("geometry")
        if not geom:
            continue
        geoms.append(shape(geom))
        rows.append(props)
    return gpd.GeoDataFrame(rows, geometry=geoms, crs=WGS84)


def load_buildings_in_bounds(path: Path, bounds: list[float]) -> gpd.GeoDataFrame:
    """Load LINZ outlines clipped to [west, south, east, north] in WGS84."""
    west, south, east, north = bounds
    bbox = (west, south, east, north)
    gdf = gpd.read_file(path, bbox=bbox)
    if gdf.empty:
        return gdf.set_crs(WGS84) if gdf.crs is None else gdf
    if gdf.crs is None:
        gdf = gdf.set_crs(WGS84)
    else:
        gdf = gdf.to_crs(WGS84)
    aoi_poly = box(west, south, east, north)
    return gdf[gdf.intersects(aoi_poly)].copy()


def intersect_buildings_with_flood(
    buildings: gpd.GeoDataFrame,
    flood: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    """Buildings that intersect flood polygons; area in NZTM2000."""
    empty_stats = {
        "buildings_in_aoi": 0,
        "buildings_affected": 0,
        "buildings_affected_pct": 0.0,
        "buildings_affected_footprint_km2": 0.0,
    }
    if buildings.empty:
        return buildings.copy(), empty_stats
    n_aoi = int(len(buildings))
    empty_stats["buildings_in_aoi"] = n_aoi
    if flood.empty:
        out = buildings.copy()
        out["flood_affected"] = False
        return out.iloc[0:0], empty_stats

    b4326 = buildings.to_crs(WGS84)
    f4326 = flood.to_crs(WGS84)
    hit = gpd.sjoin(b4326, f4326[["geometry"]], how="inner", predicate="intersects")
    if "building_i" in hit.columns:
        hit = hit.drop_duplicates(subset=["building_i"])
    else:
        hit = hit[~hit.index.duplicated(keep="first")]
    hit = hit.drop(columns=[c for c in hit.columns if c == "index_right"], errors="ignore")
    n_hit = int(len(hit))
    footprint_km2 = 0.0
    if n_hit:
        inter = gpd.overlay(
            hit.to_crs(NZTM),
            f4326.to_crs(NZTM)[["geometry"]],
            how="intersection",
            keep_geom_type=False,
        )
        if not inter.empty:
            footprint_km2 = float(inter.geometry.area.sum() / 1e6)
    stats = {
        "buildings_in_aoi": n_aoi,
        "buildings_affected": n_hit,
        "buildings_affected_pct": 100.0 * n_hit / n_aoi if n_aoi else 0.0,
        "buildings_affected_footprint_km2": footprint_km2,
    }
    hit["flood_affected"] = True
    return hit, stats


def draw_buildings_panel(
    ax,
    bounds: list[float],
    flood: gpd.GeoDataFrame,
    buildings_aoi: gpd.GeoDataFrame,
    buildings_affected: gpd.GeoDataFrame,
) -> None:
    """Map-style panel: flood fill + LINZ outlines (affected in red)."""
    from matplotlib.patches import Patch

    west, south, east, north = bounds
    if not flood.empty:
        flood.to_crs(WGS84).plot(
            ax=ax, facecolor="#ffff00", edgecolor="none", alpha=0.45, zorder=1
        )
    if not buildings_aoi.empty:
        buildings_aoi.to_crs(WGS84).plot(
            ax=ax,
            facecolor="none",
            edgecolor="#888888",
            linewidth=0.25,
            zorder=2,
        )
    if not buildings_affected.empty:
        buildings_affected.to_crs(WGS84).plot(
            ax=ax,
            facecolor="#d73027",
            edgecolor="#7f0000",
            linewidth=0.35,
            alpha=0.75,
            zorder=3,
        )
    ax.set_xlim(west, east)
    ax.set_ylim(south, north)
    ax.set_aspect("equal", adjustable="box")
    ax.set_axis_off()
    n = 0 if buildings_affected is None or buildings_affected.empty else len(buildings_affected)
    ax.set_title(f"LINZ building outlines ∩ flood\n{n:,} buildings affected")
    ax.legend(
        handles=[
            Patch(facecolor="#ffff00", edgecolor="0.2", linewidth=0.4, label="Final flood"),
            Patch(facecolor="#d73027", edgecolor="#7f0000", linewidth=0.4, label="Affected building"),
            Patch(facecolor="none", edgecolor="#888888", linewidth=0.8, label="Building in AOI"),
        ],
        loc="lower right",
        fontsize=7,
        framealpha=0.92,
    )
