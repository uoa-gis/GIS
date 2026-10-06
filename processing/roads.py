"""OSM drive network ∩ flood: likely-closed names, area, in-AOI detours.

Matches notebook section 5e. This is a geometric/network proxy, not an
official NZTA or council closure list.
"""

from __future__ import annotations

from typing import Any

import geopandas as gpd
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from shapely.ops import unary_union

from processing import defaults as cfg

NZTM = "EPSG:2193"
WGS84 = "EPSG:4326"
FLOOD_COLOR = "#ffff00"
CLOSED_COLOR = "#d01c8b"
OPEN_COLOR = "#1b9e77"


def _tag_str(val: Any, default: str = "") -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    if isinstance(val, (list, tuple, set)):
        return ", ".join(str(x) for x in val if x not in (None, ""))
    return str(val)


def _is_bridge_or_tunnel(row: pd.Series) -> bool:
    for col in ("bridge", "tunnel"):
        if col not in row.index:
            continue
        val = row[col]
        if val is True:
            return True
        text = _tag_str(val).strip().lower()
        if text in ("", "no", "false", "0", "none", "nan"):
            continue
        return True
    return False


def _half_width_m(row: pd.Series) -> float:
    min_half = float(cfg.ROAD_MIN_HALF_WIDTH_M)
    if "width" in row.index:
        raw = _tag_str(row["width"])
        if raw:
            try:
                return max(float(raw.split()[0].replace(",", ".")) / 2.0, min_half)
            except ValueError:
                pass
    n_lanes = 2.0
    if "lanes" in row.index:
        raw = _tag_str(row["lanes"])
        if raw:
            try:
                n_lanes = float(raw.split(";")[0].split("|")[0].strip())
            except ValueError:
                n_lanes = 2.0
    return max(n_lanes * float(cfg.ROAD_LANE_WIDTH_M) / 2.0, min_half)


def fetch_osm_drive_edges(bounds: list[float]) -> tuple[Any, gpd.GeoDataFrame]:
    """Download OSM drive graph for [west, south, east, north]; return graph + edges."""
    import osmnx as ox

    west, south, east, north = bounds
    ox.settings.timeout = 180
    ox.settings.use_cache = True
    try:
        graph = ox.graph_from_bbox(
            (west, south, east, north), network_type="drive", simplify=True
        )
    except TypeError:
        graph = ox.graph_from_bbox(
            north, south, east, west, network_type="drive", simplify=True
        )
    _nodes, edges = ox.graph_to_gdfs(
        graph, nodes=True, edges=True, fill_edge_geometry=True
    )
    return graph, edges.reset_index()


def empty_road_stats() -> dict[str, Any]:
    return {
        "osm_edges_in_aoi": 0,
        "osm_ground_edges": 0,
        "osm_bridge_tunnel_skipped": 0,
        "roads_in_aoi_km": 0.0,
        "roads_flooded_km": 0.0,
        "roads_flooded_area_km2": 0.0,
        "roads_likely_closed_edges": 0,
        "roads_likely_closed_names": 0,
        "roads_likely_closed_name_list": [],
        "roads_likely_unclosed_name_list": [],
        "roads_with_aoi_detour": 0,
        "roads_no_aoi_detour": 0,
        "roads_detours": [],
    }


def _ordered_via_names(graph_open, path_nodes: list, closed_name: str) -> list[str]:
    """Consecutive OSM names along a node path, excluding the closed road."""
    via_names: list[str] = []
    for a, b in zip(path_nodes[:-1], path_nodes[1:]):
        data = graph_open.get_edge_data(a, b) or {}
        ed = data[next(iter(data))] if data else {}
        nm = _tag_str(ed.get("name"), "")
        if not nm or nm == closed_name:
            continue
        if not via_names or via_names[-1] != nm:
            via_names.append(nm)
    return via_names


def _detour_record(
    name: str,
    grp: pd.DataFrame,
    status: str,
    detour_m: float | None,
    extra_m: float | None,
    via_names: list[str],
) -> dict[str, Any]:
    route = " → ".join(via_names)
    if status == "detour in AOI" and not route:
        route = "unnamed local streets"
    return {
        "name": name,
        "highway": ", ".join(sorted({h for h in grp["highway"] if h})) or "",
        "closed_edges": int(len(grp)),
        "flooded_length_m": round(float(grp["flooded_length_m"].sum()), 1),
        "flooded_area_m2": round(float(grp["flooded_area_m2"].sum()), 1),
        "flooded_area_km2": round(float(grp["flooded_area_m2"].sum() / 1e6), 5),
        "detour": status,
        "detour_length_m": None if detour_m is None else round(float(detour_m), 1),
        "extra_length_m": None if extra_m is None else round(float(extra_m), 1),
        "via_names": via_names,
        "detour_route": route,
    }


def classify_roads_with_flood(
    bounds: list[float],
    flood: gpd.GeoDataFrame,
    graph: Any | None = None,
    edges: gpd.GeoDataFrame | None = None,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame, dict[str, Any]]:
    """Return (ground edges, likely-closed, likely-open, stats)."""
    import networkx as nx

    if edges is None or graph is None:
        graph, edges = fetch_osm_drive_edges(bounds)

    roads_nztm = edges.to_crs(NZTM).copy()
    roads_nztm["name"] = roads_nztm.apply(
        lambda r: _tag_str(r["name"], "(unnamed)") if "name" in r.index else "(unnamed)",
        axis=1,
    )
    roads_nztm["highway"] = roads_nztm.apply(
        lambda r: _tag_str(r["highway"]) if "highway" in r.index else "",
        axis=1,
    )
    roads_nztm["is_bridge_or_tunnel"] = roads_nztm.apply(_is_bridge_or_tunnel, axis=1)
    roads_nztm["half_width_m"] = roads_nztm.apply(_half_width_m, axis=1)
    roads_nztm["length_m"] = roads_nztm.geometry.length

    ground = roads_nztm.loc[~roads_nztm["is_bridge_or_tunnel"]].copy()
    if ground.empty:
        stats = empty_road_stats()
        stats["osm_edges_in_aoi"] = int(len(roads_nztm))
        stats["osm_bridge_tunnel_skipped"] = int(roads_nztm["is_bridge_or_tunnel"].sum())
        empty = ground.copy()
        return ground, empty, empty, stats

    flood_empty = flood is None or flood.empty
    if flood_empty:
        flooded_len = pd.Series(0.0, index=ground.index)
        flooded_area = pd.Series(0.0, index=ground.index)
    else:
        flood_union = unary_union(flood.to_crs(NZTM).geometry)
        flooded_len = ground.geometry.intersection(flood_union).length
        buffered = gpd.GeoSeries(
            [geom.buffer(w) for geom, w in zip(ground.geometry, ground["half_width_m"])],
            index=ground.index,
            crs=ground.crs,
        )
        flooded_area = buffered.intersection(flood_union).area

    ground["flooded_length_m"] = flooded_len.to_numpy()
    ground["flooded_area_m2"] = flooded_area.to_numpy()
    ground["flooded_frac"] = ground["flooded_length_m"] / ground["length_m"].replace(0, pd.NA)
    min_len = float(cfg.ROAD_CLOSED_MIN_LENGTH_M)
    min_frac = float(cfg.ROAD_CLOSED_MIN_FRAC)
    ground["likely_closed"] = (ground["flooded_length_m"] >= min_len) | (
        ground["flooded_frac"].fillna(0) >= min_frac
    )

    roads_closed = ground.loc[ground["likely_closed"]].copy()
    roads_open = ground.loc[~ground["likely_closed"]].copy()

    closed_keys = set(
        zip(roads_closed["u"], roads_closed["v"], roads_closed["key"])
        if not roads_closed.empty
        else []
    )
    graph_open = graph.copy()
    for u, v, k in closed_keys:
        if graph_open.has_edge(u, v, k):
            graph_open.remove_edge(u, v, k)

    closed_groups = [
        (name, grp.copy())
        for name, grp in roads_closed.groupby("name", dropna=False)
    ]
    closed_groups.sort(key=lambda item: float(item[1]["flooded_length_m"].sum()), reverse=True)

    detours: list[dict[str, Any]] = []
    n_detour_ok = 0
    n_detour_none = 0
    cap = int(cfg.ROAD_MAX_DETOUR_NAMES)

    for name, grp in closed_groups[:cap]:
        longest = grp.sort_values("flooded_length_m", ascending=False).iloc[0]
        u, v = int(longest["u"]), int(longest["v"])
        orig_m = float(longest["length_m"])
        detour_m = None
        extra_m = None
        status = "no alternative in AOI"
        via_names: list[str] = []
        try:
            path_nodes = nx.shortest_path(graph_open, u, v, weight="length")
            detour_m = float(nx.path_weight(graph_open, path_nodes, weight="length"))
            extra_m = max(detour_m - orig_m, 0.0)
            status = "detour in AOI"
            n_detour_ok += 1
            via_names = _ordered_via_names(graph_open, path_nodes, name)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            n_detour_none += 1
        detours.append(_detour_record(name, grp, status, detour_m, extra_m, via_names))

    for name, grp in closed_groups[cap:]:
        detours.append(_detour_record(name, grp, "not computed (cap)", None, None, []))

    closed_name_list = [row["name"] for row in detours]
    if not roads_open.empty:
        unclosed_name_list = (
            roads_open.groupby("name")["length_m"].sum().sort_values(ascending=False).index.tolist()
        )
    else:
        unclosed_name_list = []

    stats = {
        "osm_edges_in_aoi": int(len(roads_nztm)),
        "osm_ground_edges": int(len(ground)),
        "osm_bridge_tunnel_skipped": int(roads_nztm["is_bridge_or_tunnel"].sum()),
        "roads_in_aoi_km": float(roads_nztm["length_m"].sum() / 1000.0),
        "roads_flooded_km": float(ground["flooded_length_m"].sum() / 1000.0),
        "roads_flooded_area_km2": float(ground["flooded_area_m2"].sum() / 1e6),
        "roads_likely_closed_edges": int(len(roads_closed)),
        "roads_likely_closed_names": int(len(closed_name_list)),
        "roads_likely_closed_name_list": closed_name_list,
        "roads_likely_unclosed_name_list": unclosed_name_list,
        "roads_with_aoi_detour": n_detour_ok,
        "roads_no_aoi_detour": n_detour_none,
        "roads_detours": detours,
    }
    return ground, roads_closed, roads_open, stats


def draw_roads_panel(
    ax,
    bounds: list[float] | None,
    flood: gpd.GeoDataFrame | None,
    roads_open: gpd.GeoDataFrame | None,
    roads_closed: gpd.GeoDataFrame | None,
    stats: dict[str, Any] | None = None,
) -> None:
    """Map-style panel: flood fill + OSM roads (open vs likely closed)."""
    if bounds is None:
        parts = [g for g in (flood, roads_open, roads_closed) if g is not None and not g.empty]
        if not parts:
            ax.set_axis_off()
            ax.text(0.5, 0.5, "OSM roads not available", ha="center", va="center")
            return
        west, south, east, north = parts[0].to_crs(WGS84).total_bounds
        for g in parts[1:]:
            minx, miny, maxx, maxy = g.to_crs(WGS84).total_bounds
            west, south = min(west, minx), min(south, miny)
            east, north = max(east, maxx), max(north, maxy)
    else:
        west, south, east, north = bounds
    if flood is not None and not flood.empty:
        flood.to_crs(WGS84).plot(
            ax=ax, facecolor=FLOOD_COLOR, edgecolor="none", alpha=0.45, zorder=1
        )
    n_open = 0
    if roads_open is not None and not roads_open.empty:
        n_open = (
            roads_open["name"].nunique() if "name" in roads_open.columns else len(roads_open)
        )
        roads_open.to_crs(WGS84).plot(ax=ax, color=OPEN_COLOR, linewidth=1.2, zorder=2)
    n_closed = 0
    if roads_closed is not None and not roads_closed.empty:
        n_closed = (
            roads_closed["name"].nunique() if "name" in roads_closed.columns else len(roads_closed)
        )
        roads_closed.to_crs(WGS84).plot(ax=ax, color=CLOSED_COLOR, linewidth=2.2, zorder=3)
    ax.set_xlim(west, east)
    ax.set_ylim(south, north)
    ax.set_aspect("equal", adjustable="box")
    ax.set_axis_off()
    flooded_km = 0.0 if not stats else float(stats.get("roads_flooded_km") or 0.0)
    ax.set_title(
        f"OSM roads ∩ flood\n"
        f"{n_closed} likely closed, {n_open} likely open, {flooded_km:.2f} km flooded centreline"
    )
    ax.legend(
        handles=[
            Patch(facecolor=FLOOD_COLOR, edgecolor="0.2", linewidth=0.4, label="Final flood"),
            Line2D([0], [0], color=CLOSED_COLOR, linewidth=2.2, label="Likely closed road"),
            Line2D([0], [0], color=OPEN_COLOR, linewidth=1.4, label="Likely unclosed road"),
        ],
        loc="lower right",
        fontsize=7,
        framealpha=0.92,
    )
