"""Shared AOI, dates, and GEE project for input-layer test notebooks.

Change these values once; all three layer notebooks import them.
"""

from __future__ import annotations

import math
import os

# Earth Engine Cloud project (override with EE_PROJECT if set).
GEE_PROJECT = os.environ.get("EE_PROJECT", "geog761-dongwook")

# Map centre is [lat, lon] (geemap convention). The AOI is an axis-aligned
# rectangle extending AOI_RADIUS_KM north/south/east/west of this point.

AOI_RADIUS_KM = 5.0
MAP_ZOOM = 11

# Short window so S1 and S2 can be checked quickly. Edit if a collection is empty.
# Change the dates to the dates of the flood event
# MAP_CENTER = [-39.471, 176.869]  # Hawke's Bay Airport
# START_DATE = "2023-02-01"
# END_DATE = "2023-02-25"
# FLOOD_PEAK_DATE = "2023-02-15"  # Cyclone Gabrielle (https://www.reuters.com/business/environment/cyclone-gabrielle-causes-havoc-new-zealand-firefighter-missing-2023-02-13/)


MAP_CENTER = [-38.364, 176.747]  # Hawke's Bay Airport
START_DATE = "2017-04-01"
END_DATE = "2017-04-30"
FLOOD_PEAK_DATE = "2017-04-06"

def aoi_bounds():
    """Rectangle (west, south, east, north) centred on MAP_CENTER."""
    lat, lon = MAP_CENTER
    dlat = AOI_RADIUS_KM / 111.32
    dlon = AOI_RADIUS_KM / (111.32 * math.cos(math.radians(lat)))
    return [lon - dlon, lat - dlat, lon + dlon, lat + dlat]


def aoi_geometry():
    """Earth Engine rectangle covering ±AOI_RADIUS_KM around MAP_CENTER."""
    import ee

    return ee.Geometry.Rectangle(aoi_bounds(), geodesic=False)


AOI_BOUNDS = aoi_bounds()



SLOPE_MAX_DEG = 10                # exclude steep terrain: flood-implausible, layover/shadow-prone
PERMANENT_WATER_OCCURRENCE = 50   # JRC Global Surface Water % occurrence -> "permanent" water
MMU_PIXELS = 8                    # minimum mapping unit, connected pixels at WORKING_SCALE
WORKING_SCALE = 10                # metres; matches Sentinel-1 GRD pixel spacing
SENTINEL1_ENL = 4.9                # equivalent number of looks, IW GRD (for Lee filter)
