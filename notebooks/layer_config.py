"""Shared AOI, dates, and GEE project for input-layer test notebooks.

Change these values once; all three layer notebooks import them.
"""

from __future__ import annotations

import os

# Earth Engine Cloud project (override with EE_PROJECT if set).
GEE_PROJECT = os.environ.get("EE_PROJECT", "geog761-dongwook")

# Auckland rectangle from Lecture 6 (west, south, east, north).
AOI_BOUNDS = [174.6, -36.9, 174.9, -36.6]
MAP_CENTER = [-36.8, 174.75]
MAP_ZOOM = 11

# Short window so S1 and S2 can be checked quickly. Edit if a collection is empty.
START_DATE = "2024-01-01"
END_DATE = "2024-01-31"
