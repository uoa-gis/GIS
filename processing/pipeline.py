"""Flood mapping pipeline (same flow as notebooks/01_sentinel1_layer.ipynb).

The notebook is left unchanged. This module is the server-side implementation.
"""

from __future__ import annotations

import io
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable
from urllib.request import urlopen


def s1_index_datetime(system_index: str | None) -> str:
    """Parse acquisition start from a COPERNICUS/S1_GRD ``system:index``.

    Example: ``S1A_IW_GRDH_1SDV_20230220T172223_20230220T172248_...``
    → ``2023-02-20 17:22:23 UTC``.
    """
    if not system_index:
        return "unknown"
    match = re.search(r"_(\d{8}T\d{6})_", system_index)
    if not match:
        return str(system_index)
    raw = match.group(1)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]} {raw[9:11]}:{raw[11:13]}:{raw[13:15]} UTC"


WI_VIS = {"min": -50, "max": -10, "palette": ["#08306b", "#41b6c4", "#ffffcc"]}
VV_VIS = {"min": -20, "max": 0, "palette": ["#000000", "#ffffff"]}
# ESA WorldCover v200 class code, legend label, hex colour.
WC_CLASSES: list[tuple[int, str, str]] = [
    (10, "Tree cover", "#006400"),
    (20, "Shrubland", "#ffbb22"),
    (30, "Grassland", "#ffff4c"),
    (40, "Cropland", "#f096ff"),
    (50, "Built-up", "#fa0000"),
    (60, "Bare / sparse", "#b4b4b4"),
    (70, "Snow / ice", "#f0f0f0"),
    (80, "Permanent water", "#0064c8"),
    (90, "Herbaceous wetland", "#0096a0"),
    (95, "Mangroves", "#00cf75"),
    (100, "Moss / lichen", "#fae6a0"),
]

import ee
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
from sklearn.cluster import KMeans
from sklearn.mixture import GaussianMixture

from processing.ee_auth import initialize_ee, project_id
from processing import defaults as cfg
from processing.buildings import (
    draw_buildings_panel,
    fc_payload_to_gdf,
    intersect_buildings_with_flood,
    load_buildings,
)
from processing.roads import classify_roads_with_flood, draw_roads_panel, fetch_osm_drive_edges, short_osm_error
from processing.s1_tracks import choose_track, filter_track
from processing.s1_preprocess import composite_db, terrain_geometry
from processing.map_furniture import FLOOD_COLOR, FLOOD_LABEL, add_north_arrow, add_scale_bar


def _report_section_flags() -> tuple[bool, bool, bool, bool]:
    """REPORT_* flags from processing/defaults.py (UI extra sections)."""
    return (
        bool(cfg.REPORT_ANALYSIS),
        bool(cfg.REPORT_IMPLICATIONS),
        bool(cfg.REPORT_UNCERTAINTIES),
        bool(cfg.REPORT_CONCLUSION),
    )


@dataclass
class FloodRunRequest:
    """User AOI and dates from the UI."""

    start_date: str
    end_date: str
    peak_date: str | None
    # Rectangle [west, south, east, north] or center+half_km.
    west: float | None = None
    south: float | None = None
    east: float | None = None
    north: float | None = None
    center_lat: float | None = None
    center_lon: float | None = None
    half_km: float = 5.0
    slope_max_deg: float = cfg.SLOPE_MAX_DEG
    elevation_max_m: float = cfg.ELEVATION_MAX_M

    def resolved_peak(self) -> str:
        if self.peak_date:
            return self.peak_date
        start = date.fromisoformat(self.start_date)
        end = date.fromisoformat(self.end_date)
        mid = start + (end - start) / 2
        return mid.isoformat()

    def bounds(self) -> list[float]:
        if None not in (self.west, self.south, self.east, self.north):
            return [self.west, self.south, self.east, self.north]  # type: ignore[list-item]
        if self.center_lat is None or self.center_lon is None:
            raise ValueError("Provide a drawn rectangle or a map click (lat, lon) plus half-width km.")
        lat, lon = self.center_lat, self.center_lon
        dlat = self.half_km / 111.32
        dlon = self.half_km / (111.32 * math.cos(math.radians(lat)))
        return [lon - dlon, lat - dlat, lon + dlon, lat + dlat]

    def center_latlon(self) -> tuple[float, float]:
        w, s, e, n = self.bounds()
        return ((s + n) / 2.0, (w + e) / 2.0)


@dataclass
class FloodRunResult:
    """JSON-serialisable outputs for the UI and a future Anthropic report."""

    ok: bool
    stats: dict[str, Any] = field(default_factory=dict)
    figure_png_base64: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    # Placeholder for a later Anthropic (Claude) narrative; UI can POST stats+figure.
    report: str | None = None


_ee_ready = False


def ensure_ee() -> None:
    """Initialise Earth Engine once per process (service account or ADC)."""
    global _ee_ready
    if _ee_ready:
        return
    initialize_ee()
    _ee_ready = True


# dB/linear conversion, Lee filter and terrain flattening: processing/s1_preprocess.py


def _fc_props_to_array(fc: ee.FeatureCollection, props: list[str], chunk: int) -> np.ndarray:
    """getInfo() on a FeatureCollection aborts above ~5000 elements; pull in chunks."""
    n = int(fc.size().getInfo() or 0)
    rows: list[list[float]] = []
    for start in range(0, n, chunk):
        feats = ee.FeatureCollection(fc.toList(chunk, start)).getInfo()["features"]
        for feat in feats:
            rows.append([feat["properties"].get(p) for p in props])
    return np.array(rows, dtype=float)


def _gmm_component_table(
    means_z: np.ndarray,
    wi_mean: float,
    wi_std: float,
    dw_mean: float,
    dw_std: float,
    weights: np.ndarray | None = None,
) -> list[dict[str, float]]:
    wi = means_z[:, 0] * wi_std + wi_mean
    dw = means_z[:, 1] * dw_std + dw_mean
    score = dw / dw_std - means_z[:, 0]
    table = []
    for i in range(len(wi)):
        row = {
            "wi_mean_db": float(wi[i]),
            "dwi_mean_db": float(dw[i]),
            "score": float(score[i]),
        }
        if weights is not None:
            row["weight"] = float(weights[i])
        table.append(row)
    return table


def _pick_flood_component(table: list[dict[str, float]]) -> int:
    """Flood = highest score (ΔWI/σ − WI_z) among components that darkened vs history."""
    changed = [i for i, row in enumerate(table) if row["dwi_mean_db"] > 0]
    pool = changed if changed else range(len(table))
    return max(pool, key=lambda i: table[i]["score"])


def fit_gmm_flood_cluster(
    water_index: ee.Image,
    wi_change: ee.Image,
    permanent_water: ee.Image,
    aoi: ee.Geometry,
    scale: int,
) -> tuple[ee.Image, dict[str, Any]]:
    """Unsupervised flood cluster: 2-D GMM on standardised (WI, ΔWI).

    Matches notebooks/01_sentinel1_layer.ipynb section 3. Replaces a fixed
    Otsu threshold + WI_CHANGE_MIN_DB cut: the flood component is named by
    physics (highest ΔWI/σ − WI_z among components with positive mean ΔWI), not a
    hand-set dB cutoff. WorldCover/JRC/DEM stay out of the feature vector —
    they are post-processing masks applied by the caller, same as before.

    A k-means fit on the same samples is reported in the returned meta for
    the comparison table the notebook prints; the backend does not build or
    render its per-pixel raster since it is not used downstream.
    """
    feat_stats = (
        water_index.addBands(wi_change)
        .updateMask(permanent_water.Not())
        .reduceRegion(
            reducer=ee.Reducer.mean().combine(ee.Reducer.stdDev(), sharedInputs=True),
            geometry=aoi,
            scale=scale,
            bestEffort=True,
            maxPixels=1e9,
            tileScale=4,
        )
        .getInfo()
    )
    wi_mean, wi_std = feat_stats.get("WI_mean"), feat_stats.get("WI_stdDev")
    dw_mean, dw_std = feat_stats.get("WI_change_mean"), feat_stats.get("WI_change_stdDev")
    if not wi_std or not dw_std:
        raise ValueError("Could not standardise WI / ΔWI for the GMM — check AOI coverage.")

    wi_z = water_index.subtract(wi_mean).divide(wi_std).rename("WI_z")
    dw_z = wi_change.subtract(dw_mean).divide(dw_std).rename("dWI_z")
    feat_z = wi_z.addBands(dw_z).updateMask(permanent_water.Not()).clip(aoi)

    training = feat_z.sample(
        region=aoi,
        scale=scale,
        numPixels=cfg.GMM_SAMPLE_PIXELS,
        seed=cfg.GMM_SEED,
        geometries=False,
        tileScale=4,
    )
    x = _fc_props_to_array(training, ["WI_z", "dWI_z"], cfg.GMM_GETINFO_CHUNK)
    x = x[np.isfinite(x).all(axis=1)]
    n_clusters = cfg.GMM_N_CLUSTERS
    if len(x) < n_clusters * 20:
        raise ValueError(
            f"Too few samples for the GMM ({len(x)}; need >= {n_clusters * 20}). "
            "Widen the AOI or date range."
        )

    gmm = GaussianMixture(
        n_components=n_clusters,
        covariance_type="full",
        random_state=cfg.GMM_SEED,
        n_init=5,
    ).fit(x)
    kmeans = KMeans(n_clusters=n_clusters, random_state=cfg.GMM_SEED, n_init=10).fit(x)

    gmm_table = _gmm_component_table(gmm.means_, wi_mean, wi_std, dw_mean, dw_std, gmm.weights_)
    kmeans_table = _gmm_component_table(kmeans.cluster_centers_, wi_mean, wi_std, dw_mean, dw_std)
    flood_id = _pick_flood_component(gmm_table)
    kmeans_flood_id = _pick_flood_component(kmeans_table)

    def _loglik_image(k: int) -> ee.Image:
        mean = gmm.means_[k]
        cov = gmm.covariances_[k]
        weight = float(gmm.weights_[k])
        prec = np.linalg.inv(cov)
        det = float(np.linalg.det(cov))
        log_norm = np.log(max(weight, 1e-12)) - 0.5 * np.log((2.0 * np.pi) ** 2 * max(det, 1e-12))
        d0 = wi_z.subtract(float(mean[0]))
        d1 = dw_z.subtract(float(mean[1]))
        maha = (
            d0.pow(2)
            .multiply(float(prec[0, 0]))
            .add(d0.multiply(d1).multiply(2.0 * float(prec[0, 1])))
            .add(d1.pow(2).multiply(float(prec[1, 1])))
        )
        return ee.Image.constant(log_norm).subtract(maha.multiply(0.5)).rename(f"ll_{k}")

    scores = [_loglik_image(k) for k in range(n_clusters)]
    best = scores[0]
    label = ee.Image.constant(0)
    for k, img in enumerate(scores[1:], start=1):
        label = label.where(img.gt(best), k)
        best = best.max(img)
    cluster = label.rename("cluster").clip(aoi)
    gmm_flood = cluster.eq(flood_id).rename("gmm_flood")

    meta = {
        "clusterer": "gmm",
        "n_clusters": n_clusters,
        "cluster_sample_pixels": cfg.GMM_SAMPLE_PIXELS,
        "cluster_samples_used": int(len(x)),
        "gmm_flood_id": flood_id,
        "gmm_components": gmm_table,
        "kmeans_flood_id": kmeans_flood_id,
        "kmeans_components": kmeans_table,
    }
    return gmm_flood, meta


def fabdem_elevation(aoi: ee.Geometry) -> ee.Image:
    """FABDEM: Copernicus GLO-30 with building and tree height removed (~30 m).

    GLO-30 itself is closer to a DSM (roofs/canopy). Using it as ground elevation
    inflates heights in towns and forest and biases flood depth / elev_ok.
    """
    col = ee.ImageCollection("projects/sat-io/open-datasets/FABDEM").filterBounds(aoi)
    # mosaic() drops the tile CRS; Terrain.slope needs a default projection.
    dem = col.mosaic().select("b1").setDefaultProjection(col.first().projection())
    return dem.clip(aoi).rename("elevation")


def _km2(val: Any) -> float:
    return 0.0 if val is None else float(val) / 1e6


def _stacked_pixel_area(masks: dict[str, ee.Image]) -> ee.Image:
    """One multi-band pixel-area image so area stats share a single reduceRegion."""
    px = ee.Image.pixelArea()
    bands = [px.updateMask(mask).rename(name) for name, mask in masks.items()]
    stack = bands[0]
    for band in bands[1:]:
        stack = stack.addBands(band)
    return stack


def _worldcover_rgb(worldcover: ee.Image) -> ee.Image:
    """Discrete WorldCover colours so the legend matches the thumbnail."""
    codes = [row[0] for row in WC_CLASSES]
    palette = [row[2] for row in WC_CLASSES]
    indexed = worldcover.remap(codes, list(range(len(codes))), 0)
    return indexed.visualize(min=0, max=len(codes) - 1, palette=palette)


def _add_landcover_legend(ax) -> None:
    from matplotlib.patches import Patch

    handles = [
        Patch(facecolor=color, edgecolor="0.2", linewidth=0.4, label=label)
        for _, label, color in WC_CLASSES
    ]
    handles.append(
        Patch(facecolor=FLOOD_COLOR, edgecolor="0.2", linewidth=0.4, label=FLOOD_LABEL)
    )
    ax.legend(
        handles=handles,
        loc="center left",
        bbox_to_anchor=(1.02, 0.5),
        fontsize=7,
        title="WorldCover / flood",
        framealpha=0.92,
        borderpad=0.4,
    )


def _ee_thumb_array(image: ee.Image, vis: dict | None, region: ee.Geometry, dimensions: int) -> np.ndarray:
    """EE thumbnail as an array; halves the size if EE refuses (time/memory limits)."""
    from urllib.error import HTTPError

    vis_img = image if vis is None else image.visualize(**vis)
    last: Exception | None = None
    for dims in (dimensions, max(dimensions // 2, 256)):
        url = vis_img.getThumbURL({"region": region, "dimensions": dims, "format": "png", "crs": "EPSG:4326"})
        try:
            with urlopen(url) as resp:
                return np.array(Image.open(io.BytesIO(resp.read())))
        except HTTPError as err:
            last = RuntimeError(f"Earth Engine thumbnail refused ({err.code}): {err.read().decode(errors='ignore')[:300]}")
    raise last  # type: ignore[misc]


def _add_flood_legend(ax) -> None:
    from matplotlib.patches import Patch

    ax.legend(
        handles=[
            Patch(facecolor=FLOOD_COLOR, edgecolor="0.2", linewidth=0.4, label=FLOOD_LABEL),
        ],
        loc="lower right",
        fontsize=8,
        framealpha=0.92,
    )


def _wgs84_bounds(*gdfs) -> list[float] | None:
    """[west, south, east, north] from GeoDataFrames, or None if empty."""
    extents: list[tuple[float, float, float, float]] = []
    for gdf in gdfs:
        if gdf is None or getattr(gdf, "empty", True):
            continue
        geom = gdf.to_crs("EPSG:4326").geometry
        if geom.empty:
            continue
        minx, miny, maxx, maxy = geom.total_bounds
        if all(v == v for v in (minx, miny, maxx, maxy)):  # not NaN
            extents.append((minx, miny, maxx, maxy))
    if not extents:
        return None
    west = min(e[0] for e in extents)
    south = min(e[1] for e in extents)
    east = max(e[2] for e in extents)
    north = max(e[3] for e in extents)
    return [west, south, east, north]


def _figure_png_b64(
    co_db: ee.Image,
    water_index: ee.Image,
    final_flood: ee.Image,
    aoi: ee.Geometry,
    start: str,
    end: str,
    peak: str,
    center: tuple[float, float],
    closest_id: str,
    width_km: float,
    flood_km2: float,
    aoi_km2: float,
    flooded_urban_km2: float,
    bounds: list[float] | None = None,
    flood_gdf=None,
    buildings_aoi=None,
    buildings_source: str = "LINZ",
    buildings_note: str | None = None,
    buildings_affected=None,
    n_buildings_affected: int | None = None,
    roads_open=None,
    roads_closed=None,
    road_stats: dict[str, Any] | None = None,
    road_error: str | None = None,
) -> str:
    import base64

    s1_when = s1_index_datetime(closest_id)
    flood_fill = final_flood.selfMask().visualize(palette=[FLOOD_COLOR])  # opaque: legend matches
    left_img = co_db.select("VV").visualize(**VV_VIS)
    right_img = water_index.visualize(**WI_VIS).blend(flood_fill)
    left = _ee_thumb_array(left_img, None, aoi, cfg.THUMB_DIMENSIONS)
    right = _ee_thumb_array(right_img, None, aoi, cfg.THUMB_DIMENSIONS)

    w, s, e, n = bounds if bounds is not None else (0.0, 0.0, 1.0, 1.0)
    lat0 = (s + n) / 2.0
    # Same lon/lat frame and true ground aspect on all four panels, so they line up.
    geo_aspect = 1.0 / max(math.cos(math.radians(lat0)), 1e-6)
    ground_ratio = ((e - w) * math.cos(math.radians(lat0))) / max(n - s, 1e-9)  # width / height on the ground
    ground_ratio = min(max(ground_ratio, 0.5), 2.5)
    panel_w = 6.4
    fig_h = 2 * (panel_w / ground_ratio + 1.0) + 1.6  # two map rows + their titles + suptitle/caption
    fig, axes = plt.subplots(2, 2, figsize=(13.5, fig_h), layout="constrained")
    fig.get_layout_engine().set(rect=(0, 0.07, 1, 0.93), h_pad=0.12, hspace=0.06)

    def _frame(ax) -> None:
        ax.set_xlim(w, e)
        ax.set_ylim(s, n)
        ax.set_aspect(geo_aspect, adjustable="box")

    axes[0][0].imshow(left, extent=(w, e, s, n), interpolation="nearest")
    axes[0][1].imshow(right, extent=(w, e, s, n), interpolation="nearest")
    for ax in (axes[0][0], axes[0][1]):
        _frame(ax)
    axes[0][0].set_title(f"Sentinel-1 VV (dB)\nS1 closest to peak: {s1_when}")
    axes[0][1].set_title(f"Water index (VV+VH) + flood extent\nS1 closest to peak: {s1_when}")
    axes[0][0].set_axis_off()
    axes[0][1].set_axis_off()
    km_per_deg_lon = 111.32 * math.cos(math.radians(lat0))
    for ax in (axes[0][0], axes[0][1]):
        add_scale_bar(ax, km_per_deg_lon)
        add_north_arrow(ax)
    _add_flood_legend(axes[0][1])
    bldg_bounds = bounds or _wgs84_bounds(flood_gdf, buildings_aoi, buildings_affected)
    has_buildings = (buildings_aoi is not None and not buildings_aoi.empty) or (
        buildings_affected is not None and not buildings_affected.empty
    )
    has_flood = flood_gdf is not None and not flood_gdf.empty
    if buildings_aoi is None:
        axes[1][0].set_axis_off()
        axes[1][0].text(
            0.5, 0.5,
            buildings_note or "Building outlines not loaded",
            wrap=True,
            ha="center", va="center",
        )
    elif bldg_bounds is not None and (has_buildings or has_flood):
        draw_buildings_panel(
            axes[1][0],
            bldg_bounds,
            flood_gdf,
            buildings_aoi,
            buildings_affected,
            source_label=buildings_source,
        )
    else:
        axes[1][0].set_axis_off()
        axes[1][0].text(0.5, 0.5, f"{buildings_source} buildings not available", ha="center", va="center")
    road_bounds = bounds or _wgs84_bounds(flood_gdf, roads_open, roads_closed)
    has_roads = (roads_open is not None and not roads_open.empty) or (
        roads_closed is not None and not roads_closed.empty
    )
    has_flood = flood_gdf is not None and not flood_gdf.empty
    if road_error:
        draw_roads_panel(
            axes[1][1],
            road_bounds,
            flood_gdf,
            roads_open,
            roads_closed,
            stats=road_stats,
            error=road_error,
        )
    elif road_bounds is not None and (has_roads or has_flood):
        draw_roads_panel(
            axes[1][1],
            road_bounds,
            flood_gdf,
            roads_open,
            roads_closed,
            stats=road_stats,
        )
    else:
        axes[1][1].set_axis_off()
        axes[1][1].text(0.5, 0.5, "OSM roads not available", ha="center", va="center")
    for ax in (axes[1][0], axes[1][1]):
        if ax.axison or ax.collections or ax.lines:
            _frame(ax)
    center_str = f"[{center[0]:.4f}, {center[1]:.4f}]"
    fig.suptitle(
        f"{center_str}  |  ±{width_km / 2:.1f} km  |  {start} → {end}\n"
        f"S1 scene {closest_id}",
        fontsize=10,
    )
    pct = 100 * flood_km2 / aoi_km2 if aoi_km2 else 0.0
    urban_pct = 100 * flooded_urban_km2 / flood_km2 if flood_km2 else 0.0
    extra = ""
    if n_buildings_affected is not None:
        extra += f" {buildings_source} buildings intersecting flood: {n_buildings_affected:,}."
    if road_error:
        extra += f" OSM roads not available: {road_error}"
    else:
        n_affected_names = None if not road_stats else road_stats.get("roads_flood_affected_names")
        if n_affected_names is not None:
            extra += f" OSM flood affected roads: {n_affected_names}."
    fig.text(
        0.5,
        0.01,
        f"Figure: Flood extent (olive) = {flood_km2 * 1e6:,.0f} m² "
        f"({flood_km2:,.2f} km², {pct:.1f}% of the {aoi_km2:,.1f} km² AOI). "
        f"Urban flooded: {flooded_urban_km2:,.2f} km² ({urban_pct:.1f}% of the flood)."
        f"{extra} Roads{' and buildings' if buildings_source == 'OSM' else ''} © OpenStreetMap contributors (ODbL).",
        ha="center",
        va="bottom",
        fontsize=11,
        wrap=True,
    )
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


TOTAL_STEPS = 10


def run_flood_mapping(
    req: FloodRunRequest,
    progress: Callable[[int, int, str], None] | None = None,
) -> FloodRunResult:
    """Execute the notebook-equivalent pipeline for one UI request.

    ``progress(step, total, label)`` is called before each major stage.
    """

    def report(step: int, label: str) -> None:
        if progress is not None:
            progress(step, TOTAL_STEPS, label)

    try:
        report(1, "Initialising Earth Engine")
        ensure_ee()
        start, end, peak = req.start_date, req.end_date, req.resolved_peak()
        if date.fromisoformat(end) <= date.fromisoformat(start):
            raise ValueError("end_date must be after start_date.")
        bounds = req.bounds()
        aoi = ee.Geometry.Rectangle(bounds, geodesic=False)
        scale = cfg.WORKING_SCALE
        center = req.center_latlon()

        def s1_iw_vv_vh_angle(start_d: str, end_d: str) -> ee.ImageCollection:
            return (
                ee.ImageCollection("COPERNICUS/S1_GRD")
                .filterBounds(aoi)
                .filterDate(start_d, end_d)
                .filter(ee.Filter.eq("instrumentMode", "IW"))
                .filter(ee.Filter.eq("resolution_meters", 10))
                .select(["VV", "VH", "angle"])
            )

        report(2, "Searching Sentinel-1 scenes")
        co_col = s1_iw_vv_vh_angle(start, end)
        n_co = int(co_col.size().getInfo())
        if n_co <= 0:
            raise ValueError("No Sentinel-1 scenes for this AOI and date range.")

        # One orbit track: must cover >=95% of the AOI, then closest to the peak.
        # Mixed tracks only if none covers the AOI (or MATCH_ORBIT is off).
        track = choose_track(co_col, aoi, peak, match=bool(cfg.MATCH_ORBIT))
        closest_id = track["closest_id"]
        peak_diff = float(track["peak_diff_days"])
        rel_orbit, orbit_pass = track["rel_orbit"], track["orbit_pass"]

        def same_track(col: ee.ImageCollection) -> ee.ImageCollection:
            return filter_track(col, track)

        co_col = same_track(co_col)
        if track["matched"]:
            # Event image = the single pass nearest the peak (all its slices), not an
            # average of every pass in the window, which would dilute the flood with
            # earlier or later (drained) acquisitions.
            t_peak = ee.Image(co_col.filter(ee.Filter.eq("system:index", closest_id)).first()).date()
            co_col = co_col.filterDate(t_peak.advance(-1, "hour"), t_peak.advance(1, "hour"))
        n_co = int(co_col.size().getInfo())

        def mask_s2_clouds(image: ee.Image) -> ee.Image:
            qa = image.select("QA60")
            cloud_bit = 1 << 10
            cirrus_bit = 1 << 11
            mask = qa.bitwiseAnd(cloud_bit).eq(0).And(qa.bitwiseAnd(cirrus_bit).eq(0))
            return image.updateMask(mask).divide(10000).select(["B4", "B3", "B2"])

        report(3, "Building Sentinel-2 RGB composite")
        s2_col = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(aoi)
            .filterDate(start, end)
        )
        n_s2_clear = int(s2_col.filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 10)).size().getInfo())
        s2_use = s2_col.filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 10)) if n_s2_clear else s2_col
        n_s2 = int(s2_use.size().getInfo())
        s2_rgb = None
        s2_rgb_vis = {"bands": ["B4", "B3", "B2"], "min": 0, "max": 0.3}
        if n_s2 > 0:
            s2_rgb = s2_use.map(mask_s2_clouds).median().clip(aoi)

        report(4, "Loading DEM, WorldCover and JRC permanent water")
        dem = fabdem_elevation(aoi)
        slope_deg = ee.Terrain.slope(dem).rename("slope")
        worldcover = ee.Image("ESA/WorldCover/v200/2021").clip(aoi)
        urban_flag = worldcover.eq(50).rename("urban_flag")
        wc_water = worldcover.eq(80).rename("wc_water")
        dark_land = ee.Image(0).rename("dark_land")
        for cls in cfg.EXCLUDE_WORLDCOVER_CLASSES:
            dark_land = dark_land.Or(worldcover.eq(cls))
        dark_land = dark_land.rename("dark_land")
        gsw = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").clip(aoi)
        gsw_permanent = gsw.select("occurrence").gte(cfg.PERMANENT_WATER_OCCURRENCE).unmask(0)
        ocean_no_dem = dem.mask().Not().unmask(1)
        permanent_water = gsw_permanent.Or(wc_water).Or(ocean_no_dem).rename("permanent_water")

        # Per scene: Lee filter + angular terrain flattening (Vollrath et al. 2020,
        # volume model, with geometric layover/shadow mask); then per-pixel median.
        # processing/s1_preprocess.py, shared with notebooks 01 and 04.
        tf_slope, tf_phi_s = terrain_geometry(aoi)

        def build_composite(col: ee.ImageCollection) -> ee.Image:
            return composite_db(col, aoi, tf_slope, tf_phi_s, enl=cfg.SENTINEL1_ENL)

        report(5, "Per-scene Lee filter and terrain flattening")
        co_db = build_composite(co_col)

        water_index = co_db.select("VV").add(co_db.select("VH")).rename("WI")

        hist_end = start
        hist_start = (
            date.fromisoformat(start) - timedelta(days=int(round(365.25 * cfg.HIST_LOOKBACK_YEARS)))
        ).isoformat()
        hist_col = same_track(s1_iw_vv_vh_angle(hist_start, hist_end))
        n_hist = int(hist_col.size().getInfo())
        if n_hist <= 0:
            raise ValueError(
                f"No Sentinel-1 scenes in the {cfg.HIST_LOOKBACK_YEARS}-year lookback "
                f"({hist_start} → {hist_end}). Lengthen HIST_LOOKBACK_YEARS."
            )
        hist_db = build_composite(hist_col)
        hist_wi = hist_db.select("VV").add(hist_db.select("VH")).rename("WI_hist")
        # Positive = event is darker than the historical mean (more water-like).
        wi_change = hist_wi.subtract(water_index).rename("WI_change")

        report(6, "Fitting 2-D GMM flood cluster (WI, ΔWI)")
        gmm_flood, gmm_meta = fit_gmm_flood_cluster(water_index, wi_change, permanent_water, aoi, scale)

        # GMM flood cluster minus lakes/sea and optional WorldCover 50/60.
        # WorldCover / JRC / DEM-nodata are masks only; they were not GMM features.
        flood_candidate = (
            gmm_flood.And(permanent_water.Not())
            .And(dark_land.Not())
            .rename("flood_candidate")
        )

        report(7, "Applying elevation, slope and layover masks")
        dem_pct = dem.reduceRegion(
            reducer=ee.Reducer.percentile([5, 50, 95]),
            geometry=aoi,
            scale=30,
            bestEffort=True,
            maxPixels=1e9,
        ).getInfo()
        elev_ok = dem.lt(req.elevation_max_m).unmask(1).rename("elev_ok")
        if cfg.ELEVATION_ABOVE_P5_M is not None:
            p5 = dem_pct.get("elevation_p5")
            if p5 is None:
                raise ValueError("DEM p5 is missing — check AOI has FABDEM coverage.")
            rel_max = float(p5) + cfg.ELEVATION_ABOVE_P5_M
            elev_ok = elev_ok.And(dem.lte(rel_max).unmask(1)).rename("elev_ok")

        slope_ok = slope_deg.lt(req.slope_max_deg).unmask(1).rename("slope_ok")
        # Active layover/shadow from the terrain-flattening geometry: those pixels are
        # masked in the composite, so treat "no data" as excluded.
        layover_shadow_risk = co_db.select("VV").mask().Not().rename("layover_shadow_risk")
        flood_masked = (
            flood_candidate.And(elev_ok).And(slope_ok).And(layover_shadow_risk.Not()).rename("flood_masked")
        )
        blob_size = flood_masked.selfMask().connectedPixelCount(maxSize=128, eightConnected=True)
        flood_mmu = flood_masked.updateMask(blob_size.gte(cfg.MMU_PIXELS)).unmask(0)
        final_flood = (
            flood_mmu.focal_min(radius=scale, units="meters")
            .focal_max(radius=scale, units="meters")
            .rename("flood")
        )
        report(8, "Vectorising flood boundary polygons")
        flood_boundary = final_flood.selfMask().reduceToVectors(
            geometry=aoi,
            scale=cfg.VECTOR_SCALE,
            geometryType="polygon",
            eightConnected=True,
            labelProperty="flood",
            maxPixels=1e9,
        )

        report(9, "Computing area, building, and OSM road statistics")
        area_stack = _stacked_pixel_area(
            {
                "aoi": ee.Image.constant(1).clip(aoi),
                "flood_final": final_flood,
                "flooded_urban": final_flood.And(urban_flag),
            }
        )
        area_region = area_stack.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=aoi,
            scale=cfg.AREA_SCALE,
            bestEffort=True,
            maxPixels=1e9,
        )
        def _load_buildings():
            return load_buildings(bounds, cfg.BUILDING_OUTLINES_PATH, cfg.BUILDING_SOURCE)

        with ThreadPoolExecutor(max_workers=4) as pool:
            osm_fut = pool.submit(fetch_osm_drive_edges, bounds)
            bldg_fut = pool.submit(_load_buildings)
            area_fut = pool.submit(area_region.getInfo)
            flood_fut = pool.submit(flood_boundary.getInfo)

            areas = area_fut.result() or {}
            flood_gdf = fc_payload_to_gdf(flood_fut.result())
            n_poly = int(len(flood_gdf))
            stats = {
                "aoi_km2": _km2(areas.get("aoi")),
                "flood_final_km2": _km2(areas.get("flood_final")),
                "flooded_urban_km2": _km2(areas.get("flooded_urban")),
            }
            stats["flood_pct_of_aoi"] = (
                100 * stats["flood_final_km2"] / stats["aoi_km2"] if stats["aoi_km2"] else 0.0
            )

            buildings_aoi, buildings_source, buildings_note = bldg_fut.result()
            stats["buildings_source"] = buildings_source
            buildings_affected = None
            if buildings_aoi is not None:
                buildings_affected, bldg_stats = intersect_buildings_with_flood(
                    buildings_aoi, flood_gdf
                )
                stats.update(bldg_stats)

            roads_closed = None
            roads_open = None
            road_stats: dict[str, Any] | None = None
            meta_road_error = None
            meta_road_error_detail = None
            try:
                osm_graph, osm_edges = osm_fut.result()
                stats["roads_source"] = osm_graph.graph.get("source", "OSM")
                _ground, roads_closed, roads_open, road_stats = classify_roads_with_flood(
                    bounds, flood_gdf, graph=osm_graph, edges=osm_edges
                )
                stats.update(road_stats)
            except Exception as road_exc:  # noqa: BLE001 — OSM/Overpass must not fail the flood run
                meta_road_error_detail = f"{type(road_exc).__name__}: {road_exc}"
                meta_road_error = short_osm_error(meta_road_error_detail)
                roads_closed = None
                roads_open = None
                road_stats = None

        report(10, "Rendering comparison figure")
        w, s, e, n = bounds
        width_km = abs(e - w) * 111.32 * math.cos(math.radians((s + n) / 2.0))
        figure_b64 = _figure_png_b64(
            co_db,
            water_index,
            final_flood,
            aoi,
            start,
            end,
            peak,
            center,
            str(closest_id),
            width_km,
            stats["flood_final_km2"],
            stats["aoi_km2"],
            stats["flooded_urban_km2"],
            bounds=bounds,
            flood_gdf=flood_gdf,
            buildings_aoi=buildings_aoi,
            buildings_source=buildings_source,
            buildings_note=buildings_note,
            buildings_affected=buildings_affected,
            n_buildings_affected=stats.get("buildings_affected"),
            roads_open=roads_open,
            roads_closed=roads_closed,
            road_stats=road_stats,
            road_error=meta_road_error,
        )
        meta = {
            "gee_project": project_id(),
            "bounds": bounds,
            "center_latlon": list(center),
            "start_date": start,
            "end_date": end,
            "peak_date": peak,
            "s1_scenes": n_co,
            "s1_relative_orbit": rel_orbit,
            "s1_orbit_pass": orbit_pass,
            "match_orbit": bool(cfg.MATCH_ORBIT),
            "track_matched": track["matched"],
            "track_coverage_of_aoi": track["coverage"],
            "track_message": track["message"],
            "s1_hist_scenes": n_hist,
            "s1_hist_start": hist_start,
            "s1_hist_end": hist_end,
            "hist_lookback_years": cfg.HIST_LOOKBACK_YEARS,
            "s2_scenes_used": n_s2,
            "s2_scenes_cloudy_lt_10": n_s2_clear,
            "closest_s1_id": closest_id,
            "closest_s1_datetime": s1_index_datetime(str(closest_id)),
            "closest_s1_peak_diff_days": peak_diff,
            **gmm_meta,
            "dem_percentiles": dem_pct,
            "flood_polygons": n_poly,
            "slope_max_deg": req.slope_max_deg,
            "elevation_max_m": req.elevation_max_m,
            "notebook_equivalent": "notebooks/01_sentinel1_layer.ipynb",
            "osm_road_error": meta_road_error,
            "osm_road_error_detail": meta_road_error_detail,
            "buildings_note": buildings_note,
        }
        html_report = None
        analysis, implications, uncertainties, conclusion = _report_section_flags()
        has_key = bool((os.environ.get("GEMINI_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip())
        if (analysis or implications or uncertainties or conclusion) and not has_key:
            meta["llm_report_skipped"] = "No GEMINI_KEY in .env; AI summary skipped."
        elif analysis or implications or uncertainties or conclusion:
            from llm.report import generate_flood_report

            try:
                html_report = generate_flood_report(
                    stats,
                    figure_b64,
                    meta,
                    include_analysis=analysis,
                    include_implications=implications,
                    include_uncertainties=uncertainties,
                    include_conclusion=conclusion,
                )
            except Exception as llm_exc:  # noqa: BLE001 — the AI summary must not fail the flood run
                meta["llm_report_skipped"] = f"{type(llm_exc).__name__}: {llm_exc}"
        return FloodRunResult(
            ok=True, stats=stats, figure_png_base64=figure_b64, meta=meta, report=html_report
        )
    except Exception as exc:  # noqa: BLE001 — surface EE/user errors to the UI
        return FloodRunResult(ok=False, error=str(exc))
