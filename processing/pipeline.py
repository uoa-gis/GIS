"""Flood mapping pipeline (same flow as notebooks/01_sentinel1_layer.ipynb).

The notebook is left unchanged. This module is the server-side implementation.
"""

from __future__ import annotations

import io
import math
import re
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
FLOOD_COLOR = "#ffff00"

import ee
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image

from processing import defaults as cfg


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
    stats: dict[str, float] = field(default_factory=dict)
    figure_png_base64: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    # Placeholder for a later Anthropic (Claude) narrative; UI can POST stats+figure.
    report: str | None = None


_ee_ready = False


def ensure_ee() -> None:
    """Initialise Earth Engine once per process."""
    global _ee_ready
    if _ee_ready:
        return
    ee.Initialize(project=cfg.GEE_PROJECT)
    _ee_ready = True


def to_natural(img: ee.Image) -> ee.Image:
    return ee.Image(10.0).pow(img.divide(10.0))


def to_db(img: ee.Image) -> ee.Image:
    return img.max(1e-10).log10().multiply(10.0)


def lee_filter_band(band_img: ee.Image, radius: int = 1, enl: float = cfg.SENTINEL1_ENL) -> ee.Image:
    names = band_img.bandNames()
    kernel = ee.Kernel.square(radius=radius, units="pixels")
    mean = band_img.reduceNeighborhood(ee.Reducer.mean(), kernel).rename(names)
    variance = band_img.reduceNeighborhood(ee.Reducer.variance(), kernel).rename(names)
    ci = variance.sqrt().divide(mean.max(1e-6))
    cu = enl ** -0.5
    weight = (
        ee.Image(1)
        .subtract(ee.Image(cu * cu).divide(ci.multiply(ci).max(1e-6)))
        .clamp(0, 1)
    )
    return mean.add(weight.multiply(band_img.subtract(mean)))


def lee_filter(image: ee.Image, radius: int = 1, enl: float = cfg.SENTINEL1_ENL) -> ee.Image:
    return ee.Image.cat(
        [lee_filter_band(image.select([b]), radius, enl).rename(b) for b in ["VV", "VH"]]
    )


def _otsu_from_histogram(counts: list[float], means: list[float]) -> float | None:
    total = sum(counts)
    if total == 0:
        return None
    grand_mean = sum(c * m for c, m in zip(counts, means)) / total
    best_bss, best_mean = -1.0, means[0]
    a_count = a_sum = 0.0
    for i in range(len(counts) - 1):
        a_count += counts[i]
        a_sum += counts[i] * means[i]
        b_count = total - a_count
        if a_count == 0 or b_count == 0:
            continue
        a_mean = a_sum / a_count
        b_mean = (total * grand_mean - a_sum) / b_count
        bss = a_count * (a_mean - grand_mean) ** 2 + b_count * (b_mean - grand_mean) ** 2
        if bss > best_bss:
            best_bss, best_mean = bss, means[i]
    return best_mean


def compute_otsu(
    band_image: ee.Image,
    band_name: str,
    region: ee.Geometry,
    scale: int = cfg.WORKING_SCALE,
    max_buckets: int = 256,
    min_bucket_width: float = 0.001,
) -> float:
    hist = (
        band_image.reduceRegion(
            reducer=ee.Reducer.histogram(max_buckets, min_bucket_width),
            geometry=region,
            scale=scale,
            bestEffort=True,
            maxPixels=1e9,
        )
        .get(band_name)
        .getInfo()
    )
    threshold = None
    if hist and hist.get("bucketMeans") and hist.get("histogram"):
        threshold = _otsu_from_histogram(hist["histogram"], hist["bucketMeans"])
    if threshold is None:
        threshold = (
            band_image.reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=region,
                scale=scale,
                bestEffort=True,
                maxPixels=1e9,
            )
            .get(band_name)
            .getInfo()
        )
    if threshold is None:
        raise ValueError(f"compute_otsu: no valid pixels for band '{band_name}'.")
    return float(threshold)


def _area_km2(mask_img: ee.Image, aoi: ee.Geometry) -> float:
    m2 = (
        ee.Image.pixelArea()
        .updateMask(mask_img)
        .reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=aoi,
            scale=cfg.AREA_SCALE,
            bestEffort=True,
            maxPixels=1e9,
        )
        .get("area")
    )
    val = ee.Number(m2).divide(1e6).getInfo()
    return 0.0 if val is None else float(val)


def _worldcover_rgb(worldcover: ee.Image) -> ee.Image:
    """Discrete WorldCover colours so the legend matches the thumbnail."""
    codes = [row[0] for row in WC_CLASSES]
    palette = [row[2] for row in WC_CLASSES]
    indexed = worldcover.remap(codes, list(range(len(codes))), 0)
    return indexed.visualize(min=0, max=len(codes) - 1, palette=palette)


def _nice_scale_km(width_km: float) -> float:
    target = max(width_km * 0.22, 0.5)
    candidates = (0.5, 1, 2, 5, 10, 20, 50)
    chosen = candidates[0]
    for value in candidates:
        if value <= target:
            chosen = value
    return chosen


def _add_scale_bar(ax, width_km: float) -> None:
    """Draw a scale bar in the lower-left using image pixel coordinates."""
    import matplotlib.patheffects as pe
    from matplotlib.patches import Rectangle

    img = ax.get_images()[0]
    height_px, width_px = img.get_array().shape[:2]
    scale_km = _nice_scale_km(width_km)
    bar_px = scale_km / width_km * width_px
    # imshow puts row 0 at the top, so "lower" means a large y value.
    bar_h = max(height_px * 0.012, 4)
    x0, y0 = width_px * 0.06, height_px * 0.92 - bar_h
    ax.add_patch(
        Rectangle(
            (x0, y0),
            bar_px,
            bar_h,
            transform=ax.transData,
            facecolor="white",
            edgecolor="black",
            linewidth=1.4,
            zorder=5,
        )
    )
    ax.text(
        x0 + bar_px / 2,
        y0 - height_px * 0.01,
        f"{scale_km:g} km",
        ha="center",
        va="bottom",
        fontsize=8,
        color="white",
        zorder=6,
        path_effects=[pe.withStroke(linewidth=3, foreground="black")],
    )


def _add_north_arrow(ax) -> None:
    """White north arrow with a black halo so it reads on dark SAR."""
    import matplotlib.patheffects as pe

    halo = [pe.withStroke(linewidth=3.5, foreground="black")]
    ax.annotate(
        "N",
        xy=(0.93, 0.92),
        xytext=(0.93, 0.76),
        xycoords="axes fraction",
        textcoords="axes fraction",
        ha="center",
        va="center",
        fontsize=10,
        fontweight="bold",
        color="white",
        path_effects=halo,
        arrowprops=dict(
            arrowstyle="-|>",
            color="white",
            lw=2.2,
            mutation_scale=14,
        ),
        zorder=6,
    )
    # Outline the arrow shaft (annotate arrowprops ignore path_effects).
    ax.annotate(
        "",
        xy=(0.93, 0.92),
        xytext=(0.93, 0.76),
        xycoords="axes fraction",
        textcoords="axes fraction",
        arrowprops=dict(
            arrowstyle="-|>",
            color="black",
            lw=4.2,
            mutation_scale=16,
        ),
        zorder=5,
    )


def _add_landcover_legend(ax) -> None:
    from matplotlib.patches import Patch

    handles = [
        Patch(facecolor=color, edgecolor="0.2", linewidth=0.4, label=label)
        for _, label, color in WC_CLASSES
    ]
    handles.append(
        Patch(facecolor=FLOOD_COLOR, edgecolor="0.2", linewidth=0.4, label="Final flood")
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
    vis_img = image if vis is None else image.visualize(**vis)
    url = vis_img.getThumbURL({"region": region, "dimensions": dimensions, "format": "png"})
    with urlopen(url) as resp:
        return np.array(Image.open(io.BytesIO(resp.read())))


def _add_flood_legend(ax) -> None:
    from matplotlib.patches import Patch

    ax.legend(
        handles=[
            Patch(facecolor=FLOOD_COLOR, edgecolor="0.2", linewidth=0.4, label="Final flood"),
        ],
        loc="lower right",
        fontsize=8,
        framealpha=0.92,
    )


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
) -> str:
    import base64

    s1_when = s1_index_datetime(closest_id)
    flood_fill = final_flood.selfMask().visualize(palette=[FLOOD_COLOR], opacity=0.55)
    left_img = co_db.select("VV").visualize(**VV_VIS)
    right_img = water_index.visualize(**WI_VIS).blend(flood_fill)
    left = _ee_thumb_array(left_img, None, aoi, cfg.THUMB_DIMENSIONS)
    right = _ee_thumb_array(right_img, None, aoi, cfg.THUMB_DIMENSIONS)

    fig, axes = plt.subplots(1, 2, figsize=(12.5, 6.6))
    axes[0].imshow(left)
    axes[0].set_title(f"Sentinel-1 VV (dB)\nS1 closest to peak: {s1_when}")
    axes[1].imshow(right)
    axes[1].set_title(f"Water index (VV+VH) + final flood\nS1 closest to peak: {s1_when}")
    for ax in axes:
        ax.set_axis_off()
    _add_scale_bar(axes[0], width_km)
    _add_north_arrow(axes[0])
    _add_flood_legend(axes[1])
    center_str = f"[{center[0]:.4f}, {center[1]:.4f}]"
    fig.suptitle(
        f"{center_str}  |  ±{width_km / 2:.1f} km  |  {start} → {end}\n"
        f"S1 scene {closest_id}",
        fontsize=10,
    )
    pct = 100 * flood_km2 / aoi_km2 if aoi_km2 else 0.0
    fig.text(
        0.5,
        0.01,
        f"Figure: Final flood extent (yellow) = {flood_km2 * 1e6:,.0f} m² "
        f"({flood_km2:,.2f} km², {pct:.1f}% of the {aoi_km2:,.1f} km² AOI). "
        f"Urban flooded: {flooded_urban_km2:,.2f} km² ({100 * flooded_urban_km2 / flood_km2:.1f}% of the flood)",
        ha="center",
        va="bottom",
        fontsize=12,
        wrap=True,
    )
    fig.tight_layout(rect=(0, 0.05, 1, 0.92))
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
                # .filter(ee.Filter.eq("orbitProperties_pass", "DESCENDING"))
                .filter(ee.Filter.eq("resolution_meters", 10))
                .select(["VV", "VH", "angle"])
            )

        report(2, "Searching Sentinel-1 scenes")
        co_col = s1_iw_vv_vh_angle(start, end)
        n_co = int(co_col.size().getInfo())
        if n_co <= 0:
            raise ValueError("No Sentinel-1 scenes for this AOI and date range.")

        peak_ee = ee.Date(peak)
        co_with_diff = co_col.map(
            lambda img: img.set(
                "peak_diff_days", ee.Number(img.date().difference(peak_ee, "day")).abs()
            )
        )
        closest = ee.Image(co_with_diff.sort("peak_diff_days").first())
        closest_id = closest.get("system:index").getInfo()
        peak_diff = float(closest.get("peak_diff_days").getInfo())

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
        dem = (
            ee.ImageCollection("COPERNICUS/DEM/GLO30")
            .select("DEM")
            .mosaic()
            .clip(aoi)
            .rename("elevation")
        )
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

        slope_rad = slope_deg.multiply(3.141592653589793 / 180.0)
        terrain_gain = slope_rad.cos().max(0.1).unmask(1)

        def build_composite(col: ee.ImageCollection) -> ee.Image:
            natural_mean = to_natural(col.select(["VV", "VH"]).mean()).clip(aoi)
            filtered = lee_filter(natural_mean)
            flattened = filtered.divide(terrain_gain)
            return to_db(flattened).rename(["VV", "VH"])

        report(5, "Lee speckle filter and terrain flattening")
        co_db = build_composite(co_col)
        co_angle = co_col.select("angle").mean().clip(aoi)

        water_index = co_db.select("VV").add(co_db.select("VH")).rename("WI")
        report(6, "Computing water index, Otsu, and historical WI change")
        wi_otsu = compute_otsu(water_index, "WI", aoi, scale)
        wi_cap = cfg.WATER_INDEX_MAX_DB
        wi_effective = min(wi_otsu, wi_cap)
        event_water = water_index.lt(wi_otsu).And(water_index.lt(wi_cap)).rename("event_water")
        wi_pct = water_index.reduceRegion(
            reducer=ee.Reducer.percentile([5, 50, 95]),
            geometry=aoi,
            scale=scale,
            bestEffort=True,
            maxPixels=1e9,
        ).getInfo()

        hist_end = start
        hist_start = (
            date.fromisoformat(start) - timedelta(days=int(round(365.25 * cfg.HIST_LOOKBACK_YEARS)))
        ).isoformat()
        hist_col = s1_iw_vv_vh_angle(hist_start, hist_end)
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
        wi_anomalous = wi_change.gt(cfg.WI_CHANGE_MIN_DB).rename("wi_anomalous")
        wi_change_pct = wi_change.reduceRegion(
            reducer=ee.Reducer.percentile([5, 50, 95]),
            geometry=aoi,
            scale=scale,
            bestEffort=True,
            maxPixels=1e9,
        ).getInfo()

        flood_candidate = (
            event_water.And(wi_anomalous)
            .And(permanent_water.Not())
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
                raise ValueError("DEM p5 is missing — check AOI has Copernicus GLO-30 coverage.")
            rel_max = float(p5) + cfg.ELEVATION_ABOVE_P5_M
            elev_ok = elev_ok.And(dem.lte(rel_max).unmask(1)).rename("elev_ok")

        slope_ok = slope_deg.lt(req.slope_max_deg).unmask(1).rename("slope_ok")
        layover_risk = slope_deg.gt(co_angle)
        shadow_risk = slope_deg.gt(ee.Image(90).subtract(co_angle))
        layover_shadow_risk = layover_risk.Or(shadow_risk).unmask(0).rename("layover_shadow_risk")
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
        n_poly = int(flood_boundary.size().getInfo())

        report(9, "Computing area statistics")
        aoi_km2 = _area_km2(ee.Image.constant(1).clip(aoi), aoi)
        stats = {
            "aoi_km2": aoi_km2,
            "event_water_km2": _area_km2(event_water, aoi),
            "wi_anomalous_km2": _area_km2(wi_anomalous, aoi),
            # "permanent_water_km2": _area_km2(permanent_water, aoi),
            # "worldcover_80_km2": _area_km2(wc_water, aoi),
            # "event_on_permanent_km2": _area_km2(event_water.And(permanent_water), aoi),
            # "dark_land_km2": _area_km2(dark_land, aoi),
            # "event_on_dark_land_km2": _area_km2(event_water.And(dark_land), aoi),
            "flood_candidate_km2": _area_km2(flood_candidate, aoi),
            # "after_elev_mask_km2": _area_km2(flood_candidate.And(elev_ok), aoi),
            # "after_slope_mask_km2": _area_km2(flood_candidate.And(elev_ok).And(slope_ok), aoi),
            # "after_all_masks_km2": _area_km2(flood_masked, aoi),
            # "after_mmu_km2": _area_km2(flood_mmu, aoi),
            "flood_final_km2": _area_km2(final_flood, aoi),
            # "excluded_by_elevation_km2": _area_km2(flood_candidate.And(elev_ok.Not()), aoi),
            # "excluded_by_slope_km2": _area_km2(flood_candidate.And(slope_ok.Not()), aoi),
            # "excluded_by_layover_shadow_km2": _area_km2(
            #     flood_candidate.And(layover_shadow_risk), aoi
            # ),
            "flooded_urban_km2": _area_km2(final_flood.And(urban_flag), aoi),
        }
        stats["flood_pct_of_aoi"] = (
            100 * stats["flood_final_km2"] / stats["aoi_km2"] if stats["aoi_km2"] else 0.0
        )

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
        )
        meta = {
            "gee_project": cfg.GEE_PROJECT,
            "bounds": bounds,
            "center_latlon": list(center),
            "start_date": start,
            "end_date": end,
            "peak_date": peak,
            "s1_scenes": n_co,
            "s1_hist_scenes": n_hist,
            "s1_hist_start": hist_start,
            "s1_hist_end": hist_end,
            "hist_lookback_years": cfg.HIST_LOOKBACK_YEARS,
            "wi_change_min_db": cfg.WI_CHANGE_MIN_DB,
            "wi_change_percentiles": wi_change_pct,
            "s2_scenes_used": n_s2,
            "s2_scenes_cloudy_lt_10": n_s2_clear,
            "closest_s1_id": closest_id,
            "closest_s1_datetime": s1_index_datetime(str(closest_id)),
            "closest_s1_peak_diff_days": peak_diff,
            "wi_otsu_db": round(wi_otsu, 3),
            "wi_cap_db": wi_cap,
            "wi_effective_db": round(wi_effective, 3),
            "wi_percentiles": wi_pct,
            "dem_percentiles": dem_pct,
            "flood_polygons": n_poly,
            "slope_max_deg": req.slope_max_deg,
            "elevation_max_m": req.elevation_max_m,
            "notebook_equivalent": "notebooks/01_sentinel1_layer.ipynb",
        }
        return FloodRunResult(ok=True, stats=stats, figure_png_base64=figure_b64, meta=meta, report=None)
    except Exception as exc:  # noqa: BLE001 — surface EE/user errors to the UI
        return FloodRunResult(ok=False, error=str(exc))
