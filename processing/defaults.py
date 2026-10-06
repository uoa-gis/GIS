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
ELEVATION_MAX_M = 25.0
ELEVATION_ABOVE_P5_M = 20.0
PERMANENT_WATER_OCCURRENCE = 50
MMU_PIXELS = 8
WORKING_SCALE = 30
SENTINEL1_ENL = 4.9
WATER_INDEX_MAX_DB = -32.0
HIST_LOOKBACK_YEARS = 1.0
WI_CHANGE_MIN_DB = 4.0
EXCLUDE_WORLDCOVER_CLASSES: tuple[int, ...] = ()
VECTOR_SCALE = 40
AREA_SCALE = 20
BUILDING_OUTLINES_PATH = "data/nz-building/nz-building-outlines.shp"
THUMB_DIMENSIONS = 768

# OSM likely-closed roads (notebook 5e / processing/roads.py).
ROAD_CLOSED_MIN_LENGTH_M = 50.0
ROAD_CLOSED_MIN_FRAC = 0.30
ROAD_LANE_WIDTH_M = 3.5
ROAD_MIN_HALF_WIDTH_M = 3.5
ROAD_MAX_DETOUR_NAMES = 40

# Keep in sync with notebooks/layer_config.py. UI always shows the figure
# and Key Flood Statistics; these flags add Gemini HTML sections below.
REPORT_ANALYSIS = False
REPORT_IMPLICATIONS = False
REPORT_UNCERTAINTIES = False
REPORT_CONCLUSION = False
