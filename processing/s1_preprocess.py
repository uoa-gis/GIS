"""Per-scene Sentinel-1 preprocessing and compositing (Earth Engine).

Every scene, event and historical alike, goes through the same steps before the
per-pixel median composite:

1. dB -> linear power (COPERNICUS/S1_GRD is sigma0 in dB).
2. Lee speckle filter (3x3, ENL 4.9 for IW GRD). Applied per scene, so the
   single-look noise model is correct for every image; the median of many
   filtered scenes then reduces speckle further in the baseline.
3. Angular-based radiometric terrain flattening, volume model (Vollrath,
   Mullissa & Reiche 2020, Remote Sensing 12(11):1867; as implemented in the
   GEE Sentinel-1 ARD framework, Mullissa et al. 2021, Remote Sensing
   13(10):1954). Uses DEM slope and aspect with the satellite's look direction
   and incidence angle, converts sigma0 to terrain-flattened gamma0, and masks
   active layover and shadow from that geometry.
4. Per-pixel median of the collection, back to dB.

Used by processing/pipeline.py and notebooks 01 and 04, so all three are identical.
"""

from __future__ import annotations

import math

import ee

FABDEM = "projects/sat-io/open-datasets/FABDEM"
_HALF_PI = math.pi / 2.0


def to_natural(img: ee.Image) -> ee.Image:
    return ee.Image(10.0).pow(img.divide(10.0))


def to_db(img: ee.Image) -> ee.Image:
    return img.max(1e-10).log10().multiply(10.0)


def lee_filter(image: ee.Image, bands=("VV", "VH"), radius: int = 1, enl: float = 4.9) -> ee.Image:
    """Classic adaptive Lee filter in linear power, band by band."""
    cu = enl ** -0.5
    out = []
    for b in bands:
        band = image.select([b])
        kernel = ee.Kernel.square(radius=radius, units="pixels")
        mean = band.reduceNeighborhood(ee.Reducer.mean(), kernel).rename(b)
        variance = band.reduceNeighborhood(ee.Reducer.variance(), kernel).rename(b)
        ci = variance.sqrt().divide(mean.max(1e-6))
        weight = ee.Image(1).subtract(ee.Image(cu * cu).divide(ci.multiply(ci).max(1e-6))).clamp(0, 1)
        out.append(mean.add(weight.multiply(band.subtract(mean))).rename(b))
    return ee.Image.cat(out)


def terrain_geometry(aoi: ee.Geometry, buffer_m: float = 2000) -> tuple[ee.Image, ee.Image]:
    """DEM slope (radians) and aspect angle phi_s (radians, Vollrath convention).

    FABDEM is mosaicked over a buffered AOI and NOT clipped, so slopes at the AOI
    edge are real; areas with no DEM (open sea) are treated as flat at 0 m.
    """
    col = ee.ImageCollection(FABDEM).filterBounds(aoi.buffer(buffer_m))
    elev = col.mosaic().select("b1").setDefaultProjection(col.first().projection()).unmask(0)
    slope = ee.Terrain.slope(elev).multiply(math.pi / 180.0)
    aspect = ee.Terrain.aspect(elev)  # degrees clockwise from north, 0-360
    phi_s = aspect.where(aspect.gt(180), aspect.subtract(360)).multiply(-math.pi / 180.0)
    return slope, phi_s


def _look_heading(image: ee.Image, aoi: ee.Geometry) -> ee.Number:
    """Satellite look direction (degrees, -180..180) from the incidence-angle gradient."""
    heading = ee.Terrain.aspect(image.select("angle")).reduceRegion(
        reducer=ee.Reducer.mean(), geometry=aoi, scale=1000, bestEffort=True
    ).get("aspect")
    heading = ee.Number(ee.Algorithms.If(heading, heading, 0))
    return ee.Number(ee.Algorithms.If(heading.gt(180), heading.subtract(360), heading))


def terrain_flatten(linear: ee.Image, angle_deg: ee.Image, slope: ee.Image, phi_s: ee.Image,
                    heading: ee.Number) -> ee.Image:
    """sigma0 (linear) -> terrain-flattened gamma0, with layover/shadow masked.

    Volume model: SCF = tan(90 - theta_i + alpha_r) / tan(90 - theta_i), where
    alpha_r is the terrain slope in the range (look) direction.
    """
    theta_i = angle_deg.multiply(math.pi / 180.0)
    phi_i = ee.Image.constant(heading).multiply(math.pi / 180.0)
    phi_r = phi_i.subtract(phi_s)
    alpha_r = slope.tan().multiply(phi_r.cos()).atan()  # slope steepness in range
    ninety = ee.Image.constant(_HALF_PI)
    scf = ninety.subtract(theta_i).add(alpha_r).tan().divide(ninety.subtract(theta_i).tan())
    gamma0_flat = linear.divide(theta_i.cos()).multiply(scf)
    no_layover = alpha_r.lt(theta_i)
    no_shadow = alpha_r.gt(theta_i.subtract(ninety))
    return gamma0_flat.updateMask(no_layover.And(no_shadow))


def composite_db(col: ee.ImageCollection, aoi: ee.Geometry, slope: ee.Image, phi_s: ee.Image,
                 enl: float = 4.9, flatten: bool = True) -> ee.Image:
    """Per-scene Lee + terrain flattening, then per-pixel median, in dB (VV, VH).

    Pixels in active layover or shadow are masked in every scene of a track, so
    they are masked in the composite and can never be mapped as flood.
    """

    def prep(img):
        img = ee.Image(img)
        linear = lee_filter(to_natural(img.select(["VV", "VH"])), enl=enl)
        if flatten:
            linear = terrain_flatten(linear, img.select("angle"), slope, phi_s, _look_heading(img, aoi))
        return ee.Image(linear.rename(["VV", "VH"]).copyProperties(img, ["system:time_start"]))

    median = ee.ImageCollection(col.map(prep)).median()
    return to_db(median).rename(["VV", "VH"]).clip(aoi)
