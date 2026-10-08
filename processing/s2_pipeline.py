"""Sentinel-2 U-Net flood mapping (same flow as notebooks/02_sentinel2_layer.ipynb).

Isolated from the Sentinel-1 pipeline. Flood = event U-Net water minus permanent
water, on low / flat FABDEM ground. No pre-event comparison.
"""

from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np

from processing import defaults as cfg
from processing.pipeline import FloodRunRequest, FloodRunResult, ensure_ee, fabdem_elevation

UNET_BANDS = ["B2", "B3", "B4", "B8", "B11", "B12"]
HF_REPO = "isp-uv-es/udl4fl_models"
S2_EXPORT_SCALE = 10
S2_WATER_PROB_MIN = 0.6
WATER_CLASS = 1
ProgressFn = Callable[[int, int, str], None]

_model_lock = threading.Lock()
_model_bundle: dict[str, Any] | None = None


def _progress(cb: ProgressFn | None, step: int, total: int, label: str) -> None:
    if cb:
        cb(step, total, label)


def _utm_crs(lat: float, lon: float) -> str:
    zone = int((lon + 180.0) / 6.0) + 1
    return f"EPSG:{32700 + zone if lat < 0 else 32600 + zone}"


def _l1c_col(geom, start: str, end: str):
    import ee

    return (
        ee.ImageCollection("COPERNICUS/S2_HARMONIZED")
        .filterBounds(geom)
        .filterDate(start, end)
        .select(UNET_BANDS)
    )


def _l1c_first(geom, start: str, end: str) -> tuple[Any, dict[str, Any]]:
    import ee

    col = _l1c_col(geom, start, end)
    n = col.size().getInfo()
    meta = {"start": start, "end": end, "id": None, "cloud_pct": None, "n": int(n)}
    if n == 0:
        return None, meta
    img = ee.Image(col.sort("system:time_start").first())
    props = img.getInfo()["properties"]
    meta["id"] = props.get("system:index", "?")
    meta["cloud_pct"] = props.get("CLOUDY_PIXEL_PERCENTAGE")
    return img.clip(geom), meta


def _load_unet():
    """Load sm_unet_s2 once (U-Net++ + MobileNetV2; strip ``network.`` keys)."""
    global _model_bundle
    with _model_lock:
        if _model_bundle is not None:
            return _model_bundle
        import torch
        import segmentation_models_pytorch as smp
        from huggingface_hub import hf_hub_download

        json_path = hf_hub_download(HF_REPO, "sm_unet_s2.json")
        pt_path = hf_hub_download(HF_REPO, "sm_unet_s2.pt")
        with open(json_path) as f:
            unet_cfg = json.load(f)
        hp = unet_cfg["model_params"]["hyperparameters"]
        norm = unet_cfg["model_params"]["normalization"]
        mean = np.array(norm["mean"]["S2"], dtype=np.float32)
        std = np.array(norm["std"]["S2"], dtype=np.float32)
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model = smp.UnetPlusPlus(
            encoder_name=hp["encoder"],
            encoder_weights=None,
            in_channels=int(hp["num_channels"]["S2"]),
            classes=int(hp["num_classes"]),
            activation=None,
        )
        ckpt = torch.load(pt_path, map_location=device, weights_only=False)
        state = ckpt["state_dict"] if isinstance(ckpt, dict) and "state_dict" in ckpt else ckpt
        if isinstance(state, dict) and len(state) == 1 and "model" in state:
            state = state["model"]
        cleaned: dict[str, Any] = {}
        for key, val in state.items():
            name = key
            for prefix in ("model.model.", "network.", "model.", "net.", "module."):
                if name.startswith(prefix):
                    name = name[len(prefix) :]
                    break
            if name.startswith("loss") or name.startswith("criterion"):
                continue
            cleaned[name] = val
        missing, unexpected = model.load_state_dict(cleaned, strict=False)
        if missing or unexpected:
            raise RuntimeError(
                f"sm_unet_s2 load mismatch: missing={len(missing)} unexpected={len(unexpected)}"
            )
        model.to(device)
        model.eval()
        _model_bundle = {"model": model, "device": device, "mean": mean, "std": std}
        return _model_bundle


def _predict_water_prob(chw: np.ndarray, valid: np.ndarray, bundle: dict[str, Any], tile: int = 256, overlap: int = 32) -> np.ndarray:
    import torch

    model = bundle["model"]
    device = bundle["device"]
    mean = bundle["mean"]
    std = bundle["std"]
    _, height, width = chw.shape
    x = (chw - mean[:, None, None]) / (std[:, None, None] + 1e-6)
    x[:, ~valid] = 0.0
    prob = np.zeros((height, width), dtype=np.float32)
    weight = np.zeros((height, width), dtype=np.float32)
    step = max(tile - overlap, 1)
    for row in range(0, height, step):
        for col in range(0, width, step):
            r1 = min(row + tile, height)
            c1 = min(col + tile, width)
            r0 = max(0, r1 - tile)
            c0 = max(0, c1 - tile)
            patch = x[:, r0:r1, c0:c1]
            ph, pw = patch.shape[1], patch.shape[2]
            pad_h = (32 - ph % 32) % 32
            pad_w = (32 - pw % 32) % 32
            if pad_h or pad_w:
                patch = np.pad(patch, ((0, 0), (0, pad_h), (0, pad_w)), mode="reflect")
            tensor = torch.from_numpy(np.ascontiguousarray(patch[None])).to(device)
            with torch.no_grad():
                logits = model(tensor)
                water = torch.softmax(logits, dim=1)[0, WATER_CLASS].detach().cpu().numpy()
            water = water[:ph, :pw]
            prob[r0:r1, c0:c1] += water
            weight[r0:r1, c0:c1] += 1.0
    out = prob / np.maximum(weight, 1.0)
    out[~valid] = 0.0
    return out


def _nice_scale_km(width_km: float) -> float:
    target = max(width_km * 0.22, 0.5)
    chosen = 0.5
    for value in (0.5, 1, 2, 5, 10, 20, 50):
        if value <= target:
            chosen = value
    return chosen


def _add_scale_bar(ax, width_km: float) -> None:
    import matplotlib.patheffects as pe
    from matplotlib.patches import Rectangle

    img = ax.get_images()[0]
    height_px, width_px = img.get_array().shape[:2]
    scale_km = _nice_scale_km(width_km)
    bar_px = scale_km / width_km * width_px
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
    import matplotlib.patheffects as pe

    ax.annotate(
        "",
        xy=(0.93, 0.92),
        xytext=(0.93, 0.76),
        xycoords="axes fraction",
        textcoords="axes fraction",
        arrowprops=dict(arrowstyle="-|>", color="black", lw=4.2, mutation_scale=16),
        zorder=5,
    )
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
        path_effects=[pe.withStroke(linewidth=3.5, foreground="black")],
        arrowprops=dict(arrowstyle="-|>", color="white", lw=2.2, mutation_scale=14),
        zorder=6,
    )


def _figure_png_b64(
    rgb: np.ndarray,
    event_water: np.ndarray,
    final_flood: np.ndarray,
    scene_id: str,
    width_km: float,
) -> str:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    fig, axes = plt.subplots(1, 3, figsize=(15, 5.4))
    axes[0].imshow(rgb)
    axes[0].set_title(f"Event L1C RGB\n{scene_id}")
    axes[1].imshow(rgb)
    axes[1].imshow(
        np.ma.masked_where(~event_water, event_water),
        cmap="Blues",
        alpha=0.55,
        vmin=0,
        vmax=1,
    )
    axes[1].set_title("U-Net water (event)")
    axes[2].imshow(rgb)
    axes[2].imshow(
        np.ma.masked_where(~final_flood, final_flood),
        cmap="autumn",
        alpha=0.7,
        vmin=0,
        vmax=1,
    )
    axes[2].set_title("Final flood (event − permanent − DEM)")
    for ax in axes:
        ax.set_axis_off()
    _add_scale_bar(axes[0], width_km)
    _add_north_arrow(axes[0])
    axes[1].legend(
        handles=[
            Patch(facecolor="#3182bd", edgecolor="0.2", linewidth=0.4, alpha=0.7, label="U-Net water")
        ],
        loc="lower right",
        fontsize=8,
        framealpha=0.92,
    )
    axes[2].legend(
        handles=[
            Patch(facecolor="#ffff00", edgecolor="0.2", linewidth=0.4, alpha=0.8, label="Final flood")
        ],
        loc="lower right",
        fontsize=8,
        framealpha=0.92,
    )
    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def run_s2_flood_mapping(
    req: FloodRunRequest,
    progress: ProgressFn | None = None,
) -> FloodRunResult:
    """Run the notebook-02 U-Net flow for a UI AOI and dates."""
    import ee
    import geemap
    import rasterio
    from rasterio.enums import Resampling
    from skimage.morphology import remove_small_objects

    total = 8
    try:
        _progress(progress, 1, total, "initialise Earth Engine")
        ensure_ee()
        west, south, east, north = req.bounds()
        aoi = ee.Geometry.Rectangle([west, south, east, north], geodesic=False)
        peak = req.resolved_peak()
        end_d = date.fromisoformat(req.end_date)
        lat, lon = req.center_latlon()
        event_crs = _utm_crs(lat, lon)

        _progress(progress, 2, total, "search Sentinel-2 L1C")
        s2_event, event_meta = None, None
        for extra in (0, 10, 20):
            e = (end_d + timedelta(days=extra)).isoformat()
            s2_event, event_meta = _l1c_first(aoi, peak, e)
            if s2_event is not None:
                break
        if s2_event is None:
            return FloodRunResult(
                ok=False,
                error="No Sentinel-2 L1C on or after the peak date. Widen the end date.",
            )

        _progress(progress, 3, total, "DEM / WorldCover / JRC")
        dem = fabdem_elevation(aoi)
        slope_deg = ee.Terrain.slope(dem).rename("slope")
        worldcover = ee.Image("ESA/WorldCover/v200/2021").clip(aoi)
        wc_water = worldcover.eq(80)
        gsw = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").clip(aoi)
        gsw_permanent = gsw.select("occurrence").gte(cfg.PERMANENT_WATER_OCCURRENCE).unmask(0)
        ocean_no_dem = dem.mask().Not().unmask(1)
        permanent_water = gsw_permanent.Or(wc_water).Or(ocean_no_dem).rename("permanent_water")
        elev_ok = dem.lt(req.elevation_max_m).unmask(1)
        if cfg.ELEVATION_ABOVE_P5_M is not None:
            p5 = dem.reduceRegion(
                reducer=ee.Reducer.percentile([5]),
                geometry=aoi,
                scale=30,
                bestEffort=True,
                maxPixels=1e9,
            ).get("elevation")
            p5_val = ee.Number(p5).getInfo()
            if p5_val is not None:
                elev_ok = elev_ok.And(dem.lte(ee.Number(p5_val).add(cfg.ELEVATION_ABOVE_P5_M)))
        slope_ok = slope_deg.lt(req.slope_max_deg).unmask(1)
        low_flat = elev_ok.And(slope_ok).rename("low_flat")
        ancillary = dem.addBands(slope_deg).addBands(permanent_water).addBands(low_flat).toFloat()

        _progress(progress, 4, total, "download GeoTIFFs")
        with tempfile.TemporaryDirectory(prefix="s2_unet_") as tmp:
            tmp_path = Path(tmp)
            event_tif = tmp_path / "event_l1c.tif"
            anc_tif = tmp_path / "ancillary.tif"
            geemap.download_ee_image(
                s2_event.select(UNET_BANDS),
                filename=str(event_tif),
                region=aoi,
                scale=S2_EXPORT_SCALE,
                crs=event_crs,
            )
            geemap.download_ee_image(
                ancillary,
                filename=str(anc_tif),
                region=aoi,
                scale=S2_EXPORT_SCALE,
                crs=event_crs,
            )

            _progress(progress, 5, total, "load U-Net")
            bundle = _load_unet()

            _progress(progress, 6, total, "infer water")
            with rasterio.open(event_tif) as src:
                event_chw = src.read().astype(np.float32)
                event_profile = src.profile
            event_valid = np.all(np.isfinite(event_chw), axis=0) & np.any(event_chw != 0, axis=0)
            event_prob = _predict_water_prob(event_chw, event_valid, bundle)
            event_water = (event_prob >= S2_WATER_PROB_MIN) & event_valid

            _progress(progress, 7, total, "flood mask")
            with rasterio.open(anc_tif) as src:
                anc = src.read(
                    out_shape=(src.count, event_chw.shape[1], event_chw.shape[2]),
                    resampling=Resampling.nearest,
                )
            perm = anc[2] > 0.5
            low = anc[3] > 0.5
            inundation = event_water & (~perm) & low
            final_flood = remove_small_objects(inundation, min_size=int(cfg.MMU_PIXELS))

            px_h = abs(event_profile["transform"][4])
            px_w = abs(event_profile["transform"][0])
            px_km2 = (px_h * px_w) / 1e6
            aoi_km2 = float(event_valid.sum() * px_km2)
            flood_km2 = float(final_flood.sum() * px_km2)
            water_km2 = float(event_water.sum() * px_km2)
            stats = {
                "aoi_km2": aoi_km2,
                "event_unet_water_km2": water_km2,
                "permanent_water_km2": float(perm.sum() * px_km2),
                "inundation_before_mmu_km2": float(inundation.sum() * px_km2),
                "flood_final_km2": flood_km2,
                "flood_pct_of_aoi": (100.0 * flood_km2 / aoi_km2) if aoi_km2 else 0.0,
                "event_scene": event_meta["id"],
                "event_cloud_pct": event_meta["cloud_pct"],
            }

            _progress(progress, 8, total, "render figure")
            rgb = np.clip(event_chw[[2, 1, 0]] / 3000.0, 0, 1)
            rgb = np.moveaxis(rgb, 0, -1)
            width_km = rgb.shape[1] * px_w / 1000.0
            figure = _figure_png_b64(rgb, event_water, final_flood, str(event_meta["id"]), width_km)

        return FloodRunResult(
            ok=True,
            stats=stats,
            figure_png_base64=figure,
            meta={
                "peak_date": peak,
                "crs": event_crs,
                "water_prob_min": S2_WATER_PROB_MIN,
                "model": "sm_unet_s2",
            },
        )
    except Exception as exc:
        return FloodRunResult(ok=False, error=str(exc))
