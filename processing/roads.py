"""OSM drive network ∩ flood: flood-affected names, area, in-AOI detours.

Matches notebook section 5e. This is a geometric/network proxy, not an
official NZTA or council closure list.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
import threading
import time
from typing import Any, Callable

import geopandas as gpd
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from shapely.geometry import LineString, box
from shapely.ops import unary_union

from processing import defaults as cfg

NZTM = "EPSG:2193"
WGS84 = "EPSG:4326"
from processing.map_furniture import FLOOD_COLOR, FLOOD_LABEL  # noqa: E402
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


_OVERPASS_URLS = (
    "https://overpass-api.de/api",
    "https://overpass.kumi.systems/api",
)
# Try order: main server, then the mirror (180 s each, as before the lock was added).
_OVERPASS_ATTEMPTS = _OVERPASS_URLS
_OVERPASS_TIMEOUT_S = 180
_OVERPASS_RETRY_PAUSE_S = 5
# Public Overpass servers limit concurrent requests per user, and osmnx settings
# are global. Roads and buildings therefore run one at a time behind this lock.
_OVERPASS_LOCK = threading.Lock()
# After every server has failed, skip Overpass for this long instead of waiting out
# the timeouts again (roads then buildings could otherwise wait ~12 minutes).
_OVERPASS_COOLDOWN_S = 600
_overpass_down_until = 0.0


def overpass_call(fn: Callable[[], Any], what: str) -> Any:
    """Run ``fn`` (an osmnx download) against each Overpass server in turn.

    ``InsufficientResponseError`` (nothing mapped in the AOI) is re-raised at once
    so the caller can treat it as an empty result.
    """
    import osmnx as ox

    global _overpass_down_until
    with _OVERPASS_LOCK:
        if time.time() < _overpass_down_until:
            raise RuntimeError(
                f"OSM Overpass failed for {what}: skipped, servers timed out on an earlier request (ReadTimeout)"
            )
        ox.settings.use_cache = True
        ox.settings.requests_timeout = _OVERPASS_TIMEOUT_S
        # Skip osmnx's slot-wait; the attempt list and lock handle pacing instead.
        ox.settings.overpass_rate_limit = False
        last_exc: Exception | None = None
        for i, url in enumerate(_OVERPASS_ATTEMPTS):
            if i:
                time.sleep(_OVERPASS_RETRY_PAUSE_S)
            ox.settings.overpass_url = url
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 — try the next server
                if type(exc).__name__ == "InsufficientResponseError":
                    raise
                last_exc = exc
        _overpass_down_until = time.time() + _OVERPASS_COOLDOWN_S
    raise RuntimeError(f"OSM Overpass failed for {what}: {type(last_exc).__name__}: {last_exc}") from last_exc


def short_osm_error(exc: BaseException | str | None) -> str:
    """One readable line for figures; full text stays in run metadata."""
    text = str(exc or "")
    if "Timeout" in text or "timed out" in text:
        return "OpenStreetMap server timed out. Try the run again in a few minutes."
    if "429" in text or "Too Many Requests" in text:
        return "OpenStreetMap server busy (rate limit). Try again in a few minutes."
    if "ConnectionError" in text or "Max retries" in text:
        return "Could not reach the OpenStreetMap server (check the internet connection)."
    first = text.splitlines()[0] if text else "unknown error"
    return first[:160]


OSM_ROADS_DIR = Path(__file__).resolve().parents[1] / "data" / "osm-roads"
_NODE_DECIMALS = 7  # ~1 cm; OSM ways that share a node have identical coordinates


def load_local_osm_roads(bounds: list[float]) -> gpd.GeoDataFrame | None:
    """Drivable road centrelines in the AOI from local Geofabrik extracts, or None.

    Files come from scripts/download_osm.py (WGS84). Lines are clipped to the AOI
    so the network matches what osmnx returns for a bbox. None means no local
    file has roads here, so the caller should try the live server.
    """
    files = sorted(OSM_ROADS_DIR.glob("*.gpkg"))
    if not files:
        return None
    parts = []
    for path in files:
        g = gpd.read_file(path, bbox=tuple(bounds))
        if not g.empty:
            parts.append(g.to_crs("EPSG:4326"))
    if not parts:
        return None
    lines = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), geometry="geometry", crs="EPSG:4326")
    lines = gpd.clip(lines, box(*bounds)).explode(index_parts=False)
    lines = lines[lines.geometry.geom_type == "LineString"]
    return lines.reset_index(drop=True) if not lines.empty else None


def build_drive_graph(lines: gpd.GeoDataFrame) -> tuple[Any, gpd.GeoDataFrame]:
    """Routable MultiDiGraph + edge table from road centrelines (osmnx-compatible).

    Lines are split at junctions: any vertex shared by two or more ways, plus way
    endpoints. Bridges over roads do not share an OSM node, so they do not create
    false junctions. One-way roads (Geofabrik ``oneway`` F/T) get one direction,
    others both. Edge ``length`` is metres in the local UTM zone, as osmnx does.
    """
    import math
    from collections import Counter

    import networkx as nx
    from pyproj import Transformer

    metric = lines.estimate_utm_crs()
    to_m = Transformer.from_crs("EPSG:4326", metric, always_xy=True)

    def key(c):
        return (round(c[0], _NODE_DECIMALS), round(c[1], _NODE_DECIMALS))

    coords_list = []
    counts: Counter = Counter()
    for geom in lines.geometry:
        cs = [key(c) for c in geom.coords]
        cs = [cs[0]] + [c for prev, c in zip(cs, cs[1:]) if c != prev]
        coords_list.append(cs)
        if len(cs) < 2:
            continue
        counts[cs[0]] += 2
        counts[cs[-1]] += 2
        for c in set(cs[1:-1]):
            counts[c] += 1

    node_id: dict[tuple[float, float], int] = {}

    def nid(c) -> int:
        if c not in node_id:
            node_id[c] = len(node_id)
        return node_id[c]

    graph = nx.MultiDiGraph(crs="EPSG:4326")
    rows: list[dict[str, Any]] = []
    records = lines.drop(columns="geometry").to_dict("records")
    for rec, cs in zip(records, coords_list):
        if len(cs) < 2:
            continue
        oneway = str(rec.get("oneway") or "B").strip().upper()
        attrs = {
            "osmid": rec.get("osm_id"),
            "name": rec.get("name"),
            "highway": rec.get("highway"),
            "ref": rec.get("ref"),
            "bridge": rec.get("bridge", "no"),
            "tunnel": rec.get("tunnel", "no"),
            "oneway": oneway,
        }
        start = 0
        for j in range(1, len(cs)):
            if j < len(cs) - 1 and counts[cs[j]] < 2:
                continue
            part = cs[start : j + 1]
            start = j
            xs, ys = to_m.transform([c[0] for c in part], [c[1] for c in part])
            length = sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2 in zip(xs, ys, xs[1:], ys[1:]))
            if length <= 0:
                continue
            u, v = nid(part[0]), nid(part[-1])
            if oneway == "F":
                directions = [(u, v, part)]
            elif oneway == "T":
                directions = [(v, u, part[::-1])]
            else:
                directions = [(u, v, part), (v, u, part[::-1])]
            for a, b, pts in directions:
                geom = LineString(pts)
                k = graph.add_edge(a, b, length=length, geometry=geom, **attrs)
                rows.append({"u": a, "v": b, "key": k, "length": length, **attrs, "geometry": geom})

    nx.set_node_attributes(graph, {i: {"x": c[0], "y": c[1]} for c, i in node_id.items()})
    edges = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    return graph, edges


def fetch_osm_drive_edges(bounds: list[float]) -> tuple[Any, gpd.GeoDataFrame]:
    """OSM drive graph + edges for [west, south, east, north].

    Local Geofabrik extracts (data/osm-roads/, scripts/download_osm.py) first, so
    runs work offline; the live Overpass server only when no local file covers
    the AOI. ``graph.graph["source"]`` records which was used.
    """
    local = load_local_osm_roads(bounds)
    if local is not None:
        graph, edges = build_drive_graph(local)
        graph.graph["source"] = "OSM local extract (Geofabrik)"
        return graph, edges

    import osmnx as ox

    west, south, east, north = bounds

    def _download():
        try:
            graph = ox.graph_from_bbox((west, south, east, north), network_type="drive", simplify=True)
        except TypeError:  # osmnx 1.x signature
            graph = ox.graph_from_bbox(north, south, east, west, network_type="drive", simplify=True)
        _nodes, edges = ox.graph_to_gdfs(graph, nodes=True, edges=True, fill_edge_geometry=True)
        return graph, edges.reset_index()

    graph, edges = overpass_call(_download, "roads")
    graph.graph["source"] = "OSM live (Overpass)"
    return graph, edges


def empty_road_stats() -> dict[str, Any]:
    return {
        "osm_edges_in_aoi": 0,
        "osm_ground_edges": 0,
        "osm_bridge_tunnel_skipped": 0,
        "roads_in_aoi_km": 0.0,
        "roads_flooded_km": 0.0,
        "roads_flooded_area_km2": 0.0,
        "roads_flood_affected_edges": 0,
        "roads_flood_affected_names": 0,
        "roads_flood_affected_name_list": [],
        "roads_not_flood_affected_name_list": [],
        "roads_with_aoi_detour": 0,
        "roads_no_aoi_detour": 0,
        "roads_detours": [],
    }


def _ordered_via_names(graph_open, path_nodes: list, closed_name: str) -> list[str]:
    """Consecutive OSM names along a node path, excluding the flood-affected road."""
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
        "flood_affected_edges": int(len(grp)),
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
    """Return (ground edges, flood-affected, not flood-affected, stats)."""
    import networkx as nx

    if edges is None or graph is None:
        graph, edges = fetch_osm_drive_edges(bounds)

    # Local UTM zone: NZTM2000 only suits NZ; UTM works for the Pacific too.
    metric_crs = edges.estimate_utm_crs() or NZTM
    roads_nztm = edges.to_crs(metric_crs).copy()
    if "name" in roads_nztm.columns:
        roads_nztm["name"] = roads_nztm["name"].map(lambda v: _tag_str(v, "(unnamed)"))
    else:
        roads_nztm["name"] = "(unnamed)"
    if "highway" in roads_nztm.columns:
        roads_nztm["highway"] = roads_nztm["highway"].map(_tag_str)
    else:
        roads_nztm["highway"] = ""
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
        flood_geom = flood.to_crs(metric_crs).geometry
        flood_union = (
            flood_geom.union_all() if hasattr(flood_geom, "union_all") else unary_union(flood_geom)
        )
        flooded_len = ground.geometry.intersection(flood_union).length
        buffered = ground.geometry.buffer(ground["half_width_m"])
        flooded_area = buffered.intersection(flood_union).area

    ground["flooded_length_m"] = flooded_len.to_numpy()
    ground["flooded_area_m2"] = flooded_area.to_numpy()
    ground["flooded_frac"] = ground["flooded_length_m"] / ground["length_m"].replace(0, pd.NA)
    min_len = float(cfg.ROAD_CLOSED_MIN_LENGTH_M)
    min_frac = float(cfg.ROAD_CLOSED_MIN_FRAC)
    ground["flood_affected"] = (ground["flooded_length_m"] >= min_len) | (
        ground["flooded_frac"].fillna(0) >= min_frac
    )

    roads_closed = ground.loc[ground["flood_affected"]].copy()
    roads_open = ground.loc[~ground["flood_affected"]].copy()

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
        "roads_flood_affected_edges": int(len(roads_closed)),
        "roads_flood_affected_names": int(len(closed_name_list)),
        "roads_flood_affected_name_list": closed_name_list,
        "roads_not_flood_affected_name_list": unclosed_name_list,
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
    error: str | None = None,
) -> None:
    """Map-style panel: flood fill + OSM roads (flood-affected vs not)."""
    if error:
        ax.set_axis_off()
        ax.set_title("OSM roads ∩ flood")
        wrapped = "\n".join(textwrap.wrap(str(error).strip(), width=56)) or str(error)
        ax.text(
            0.5,
            0.52,
            "OSM roads not available\n\n" + wrapped,
            ha="center",
            va="center",
            transform=ax.transAxes,
            fontsize=8,
        )
        return
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
            ax=ax, facecolor=FLOOD_COLOR, edgecolor="none", zorder=1
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
        f"{n_closed} flood affected, {n_open} not flood affected, "
        f"{flooded_km:.2f} km flooded centreline"
    )
    ax.legend(
        handles=[
            Patch(facecolor=FLOOD_COLOR, edgecolor="0.2", linewidth=0.4, label=FLOOD_LABEL),
            Line2D([0], [0], color=CLOSED_COLOR, linewidth=2.2, label="Flood affected road"),
            Line2D([0], [0], color=OPEN_COLOR, linewidth=1.4, label="Not flood affected road"),
        ],
        loc="lower right",
        fontsize=7,
        framealpha=0.92,
    )
