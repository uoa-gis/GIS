"""Processing thresholds only.

AOI coordinates and event dates are supplied by the UI, not this module.
Numeric defaults match notebooks/layer_config.py.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# Earth Engine Cloud project (override with EE_PROJECT in .env).
GEE_PROJECT = os.environ.get("EE_PROJECT", "geog761-dongwook")

SLOPE_MAX_DEG = 10
# Absolute cap off by default (9000 m > any terrain): a fixed 25 m removed real
# flooding on inland floodplains. ELEVATION_ABOVE_P5_M (relative cap) still applies.
ELEVATION_MAX_M = 9000.0
ELEVATION_ABOVE_P5_M = 20.0
PERMANENT_WATER_OCCURRENCE = 50
MMU_PIXELS = 8
WORKING_SCALE = 30
SENTINEL1_ENL = 4.9
HIST_LOOKBACK_YEARS = 1.0
# Event + historical stacks use only the track of the scene closest to the peak.
MATCH_ORBIT = True
EXCLUDE_WORLDCOVER_CLASSES: tuple[int, ...] = ()

# Unsupervised flood cluster: 2-D GMM on standardised (WI, ΔWI). Replaces a
# fixed Otsu threshold + WI_CHANGE_MIN_DB cut — see
# notebooks/01_sentinel1_layer.ipynb section 3. k-means is fit for the same
# comparison table the notebook prints; the backend does not render it.
GMM_N_CLUSTERS = 4
GMM_SAMPLE_PIXELS = 8000
GMM_SEED = 0
# getInfo() on a FeatureCollection aborts above ~5000 elements; pull in chunks.
GMM_GETINFO_CHUNK = 4000
VECTOR_SCALE = 40
AREA_SCALE = 20
BUILDING_OUTLINES_PATH = "data/nz-building/nz-building-outlines.shp"
# For all of NZ export the whole LINZ layer as GeoPackage to
# data/nz-building/nz-building-outlines.gpkg (found automatically; WGS84 or NZTM2000).
# "auto" = LINZ if the file above exists, else OpenStreetMap buildings (global).
# "linz" | "osm" | "none" force one source or switch building exposure off.
BUILDING_SOURCE = "auto"
THUMB_DIMENSIONS = 768

# OSM flood-affected roads (notebook 5e / processing/roads.py).
ROAD_CLOSED_MIN_LENGTH_M = 50.0
ROAD_CLOSED_MIN_FRAC = 0.30
ROAD_LANE_WIDTH_M = 3.5
ROAD_MIN_HALF_WIDTH_M = 3.5
ROAD_MAX_DETOUR_NAMES = 40

# UI always shows the figure and Key Flood Statistics; these flags add Gemini
# HTML sections below. The notebook uses its own flags in layer_config.py.
REPORT_ANALYSIS = True
REPORT_IMPLICATIONS = False
REPORT_UNCERTAINTIES = False
REPORT_CONCLUSION = False
