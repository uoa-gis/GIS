"""Processing thresholds only.

AOI coordinates and event dates are supplied by the UI, not this module.
Numeric defaults match notebooks/layer_config.py.
"""

from __future__ import annotations

import os

# Earth Engine Cloud project (override with EE_PROJECT).
GEE_PROJECT = os.environ.get("EE_PROJECT", "geog761-dongwook")

SLOPE_MAX_DEG = 10
ELEVATION_MAX_M = 25.0
ELEVATION_ABOVE_P5_M = 20.0
PERMANENT_WATER_OCCURRENCE = 50
MMU_PIXELS = 8
WORKING_SCALE = 10
SENTINEL1_ENL = 4.9
WATER_INDEX_MAX_DB = -32.0
EXCLUDE_WORLDCOVER_CLASSES: tuple[int, ...] = ()
VECTOR_SCALE = 40
AREA_SCALE = 20
THUMB_DIMENSIONS = 768
