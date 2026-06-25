"""Boundary resolution and Overpass downloads of OSM power features."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

import geopandas as gpd
import numpy as np
import osmnx as ox
import pandas as pd
from shapely.geometry import LineString, MultiPoint, MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

from common import CACHE_DIR, cache_get, cache_key, cache_set

NATURAL_EARTH_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_50m_admin_0_countries.geojson"
)
NATURAL_EARTH_PATH = CACHE_DIR / "ne_50m_admin_0_countries.geojson"
CONTINENT_NAMES = {
    "africa",
    "antarctica",
    "asia",
    "europe",
    "north america",
    "oceania",
    "south america",
}

# Aggregate region names that combine multiple Natural Earth continents.
CONTINENT_AGGREGATES: dict[str, frozenset[str]] = {
    "global": frozenset({"africa", "asia", "europe", "north america", "south america"}),
}


def _load_natural_earth_countries() -> gpd.GeoDataFrame:
    if not NATURAL_EARTH_PATH.exists():
        import urllib.request

        print(f"Downloading Natural Earth admin-0 dataset → {NATURAL_EARTH_PATH}")
        urllib.request.urlretrieve(NATURAL_EARTH_URL, NATURAL_EARTH_PATH)
    return gpd.read_file(NATURAL_EARTH_PATH)


def _continent_boundary(continent: str) -> gpd.GeoDataFrame:
    countries = _load_natural_earth_countries()
    key = continent.lower()
    aggregate = CONTINENT_AGGREGATES.get(key)
    if aggregate is not None:
        match = countries["CONTINENT"].str.lower().isin(aggregate)
    else:
        match = countries["CONTINENT"].str.lower() == key

    if key == "global":
        # Oceania is excluded from the aggregate above; pull in Australia, Papua
        # New Guinea, and New Zealand explicitly so the poster covers them
        # without dragging in the wider Pacific.
        match = match | countries["ISO_A3"].isin(["AUS", "PNG", "NZL"])

    subset = countries[match]
    if subset.empty:
        raise RuntimeError(f"No countries found for continent '{continent}' in Natural Earth")
    merged = unary_union(subset.geometry)

    if key == "global":
        # Clip the global aggregate to a tight bounding box:
        #   • north - Alaska's northernmost point (~71.4°N), to drop the empty
        #     Canadian Arctic, Greenland's interior, and Svalbard.
        #   • west - the Alaska mainland's western edge (~168.1°W), to drop the
        #     Aleutian chain and the empty Bering Sea that otherwise stretch out
        #     to the antimeridian.
        #   • east - New Zealand's easternmost main-island longitude (~178.5°E),
        #     to drop Russia's far-eastern Chukotka sliver that otherwise pushes
        #     the viewport out to the antimeridian.
        us = countries[countries["ISO_A3"] == "USA"]
        nz = countries[countries["ISO_A3"] == "NZL"]
        if us.empty or nz.empty:
            raise RuntimeError(
                "Natural Earth dataset is missing USA or NZL - cannot build global clip"
            )
        # The Alaska mainland is the USA polygon reaching the northernmost
        # latitude; it anchors both the north and west bounds of the clip.
        us_geom = unary_union(us.geometry)
        us_polys = list(us_geom.geoms) if isinstance(us_geom, MultiPolygon) else [us_geom]
        alaska = max(us_polys, key=lambda poly: poly.bounds[3])
        west_lon = float(alaska.bounds[0])
        north_lat = float(alaska.bounds[3])
        east_lon = float(nz.total_bounds[2])
        merged = merged.intersection(box(west_lon, -90, east_lon, north_lat))

    return gpd.GeoDataFrame({"name": [continent]}, geometry=[merged], crs=countries.crs)


def keep_main_landmass(geometry: Any) -> Any:
    """Drop disjoint polygons that are far from the main landmass.

    Geocoded country boundaries include overseas territories - e.g. Aruba and
    Curaçao for the Netherlands, French Guiana and Réunion for France. We keep
    the largest polygon plus any polygon whose envelope intersects a 3×-inflated
    bounding box of the largest one. This preserves close-by islands such as
    Northern Ireland, Corsica, or Japan's main islands.
    """
    if not isinstance(geometry, MultiPolygon):
        return geometry

    polygons = list(geometry.geoms)
    if len(polygons) <= 1:
        return geometry

    largest = max(polygons, key=lambda p: p.area)
    minx, miny, maxx, maxy = largest.bounds
    width = max(maxx - minx, 0.01)
    height = max(maxy - miny, 0.01)
    region = box(minx - width, miny - height, maxx + width, maxy + height)

    kept = [p for p in polygons if region.intersects(p)]
    if len(kept) == 1:
        return kept[0]
    return MultiPolygon(kept)


def load_boundary_from_geojson(
    path: Path, name: str, keep_internal_borders: bool = False
) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path)
    if gdf.empty:
        raise RuntimeError(f"Boundary file '{path}' contains no features")
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])]
    if gdf.empty:
        raise RuntimeError(f"Boundary file '{path}' contains no polygonal geometry")
    if gdf.crs is None:
        print(f"Boundary file '{path}' has no CRS - assuming EPSG:4326")
        gdf = gdf.set_crs("EPSG:4326")
    else:
        gdf = gdf.to_crs("EPSG:4326")
    if keep_internal_borders:
        # Keep every input polygon as its own part instead of dissolving with
        # unary_union. Dissolving erases edges shared by adjacent features (e.g.
        # province borders), which the caller wants drawn on the poster.
        # Collecting the exploded polygons into one MultiPolygon preserves those
        # internal borders while still returning a single boundary geometry; the
        # clip helpers re-union the parts, so coverage and extent are unchanged.
        parts = [g for g in gdf.geometry.explode(index_parts=False) if not g.is_empty]
        merged = parts[0] if len(parts) == 1 else MultiPolygon(parts)
    else:
        merged = unary_union(gdf.geometry)
    return gpd.GeoDataFrame({"name": [name]}, geometry=[merged], crs="EPSG:4326")


def get_country_boundary(country: str, mainland_only: bool = True, use_cache: bool = True) -> gpd.GeoDataFrame:
    key = cache_key("boundary_v3", country, mainland_only)
    if use_cache:
        cached = cache_get(key)
        if cached is not None:
            print(f"Using cached boundary for {country}")
            return cached

    if country.lower() in CONTINENT_NAMES or country.lower() in CONTINENT_AGGREGATES:
        print(f"Building continent boundary from Natural Earth: {country}")
        boundary = _continent_boundary(country)
    else:
        print(f"Geocoding country boundary: {country}")
        boundary = ox.geocode_to_gdf(country)
        boundary = boundary[boundary.geometry.type.isin(["Polygon", "MultiPolygon"])]
        if boundary.empty:
            raise RuntimeError(f"Could not resolve a country boundary for '{country}'")
        if mainland_only:
            merged = unary_union(boundary.geometry)
            filtered = keep_main_landmass(merged)
            before = len(merged.geoms) if isinstance(merged, MultiPolygon) else 1
            after = len(filtered.geoms) if isinstance(filtered, MultiPolygon) else 1
            if after < before:
                print(
                    f"Mainland-only: dropped {before - after} outlying polygon(s); "
                    "pass --include-outlying to keep them"
                )
            boundary = gpd.GeoDataFrame(
                {"name": [country]}, geometry=[filtered], crs=boundary.crs
            )

    cache_set(key, boundary)
    return boundary


def _polygon_to_overpass_poly(polygon: Polygon, precision: int = 6) -> str:
    """Convert a Shapely Polygon exterior ring to Overpass poly: coordinate string."""
    parts = []
    for lon, lat in polygon.exterior.coords:
        parts.append(f"{lat:.{precision}f} {lon:.{precision}f}")
    return " ".join(parts)


def _simplify_boundary_for_overpass(
    geometry: Polygon | MultiPolygon,
    max_coords: int = 2000,
) -> list[Polygon]:
    """Progressively simplify a boundary so the total coordinate count fits Overpass."""
    if isinstance(geometry, Polygon):
        polygons = [geometry]
    else:
        polygons = list(geometry.geoms)

    for tolerance in (0.005, 0.01, 0.02, 0.05, 0.1):
        total_coords = sum(len(p.exterior.coords) for p in polygons)
        if total_coords <= max_coords:
            break
        simplified = []
        for p in polygons:
            s = p.simplify(tolerance, preserve_topology=True)
            if not s.is_empty and isinstance(s, Polygon):
                simplified.append(s)
            elif not s.is_empty and isinstance(s, MultiPolygon):
                simplified.extend(s.geoms)
        polygons = simplified

    return [p for p in polygons if not p.is_empty]


def fetch_power_features_single(
    country: str,
    boundary: gpd.GeoDataFrame,
    include_minor_lines: bool = False,
    include_cables: bool = False,
    sea_buffer_km: float = 0.0,
    render_crs: str = "EPSG:3857",
    use_cache: bool = True,
    timeout: int = 300,
) -> gpd.GeoDataFrame:
    """Fetch all power features in one Overpass query using poly: filter."""
    import requests as http_requests

    values = power_tag_values(include_minor_lines, include_cables)
    key = cache_key("power_single_v1", country, values, sea_buffer_km)
    if use_cache:
        cached = cache_get(key)
        if cached is not None:
            print(f"Using cached power features for {country}")
            return cached

    boundary_geom = unary_union(boundary.geometry)

    if sea_buffer_km > 0:
        boundary_proj = boundary.to_crs(render_crs)
        buffered = unary_union(boundary_proj.geometry).buffer(sea_buffer_km * 1000)
        boundary_geom = gpd.GeoDataFrame(
            geometry=[buffered], crs=render_crs
        ).to_crs("EPSG:4326").geometry.iloc[0]

    polygons = _simplify_boundary_for_overpass(boundary_geom)
    total_coords = sum(len(p.exterior.coords) for p in polygons)
    print(
        f"Single Overpass query: {len(polygons)} polygon(s), "
        f"{total_coords:,} coordinate pairs"
    )

    power_regex = "^(" + "|".join(values) + ")$"
    way_clauses = []
    for poly in polygons:
        ps = _polygon_to_overpass_poly(poly)
        way_clauses.append(f'  way["power"~"{power_regex}"](poly:"{ps}");')

    query = (
        f"[out:json][timeout:{timeout}];\n"
        "(\n"
        + "\n".join(way_clauses) + "\n"
        ");\n"
        "out geom;\n"
    )

    overpass_url = ox.settings.overpass_url.rstrip("/")
    if not overpass_url.endswith("/interpreter"):
        overpass_url += "/interpreter"

    print(f"Sending Overpass query ({len(query):,} bytes) to {overpass_url}")
    response = http_requests.post(
        overpass_url,
        data={"data": query},
        timeout=timeout + 30,
        headers={"User-Agent": "GridToPoster/1.0"},
    )
    response.raise_for_status()
    data = response.json()

    elements = data.get("elements", [])
    print(f"Received {len(elements):,} elements from Overpass")

    rows = []
    for elem in elements:
        if elem.get("type") != "way":
            continue
        geom_coords = elem.get("geometry", [])
        if len(geom_coords) < 2:
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in geom_coords]
        tags = elem.get("tags", {})
        rows.append({
            "power": tags.get("power"),
            "voltage": tags.get("voltage"),
            "name": tags.get("name"),
            "operator": tags.get("operator"),
            "geometry": LineString(coords),
        })

    if not rows:
        raise RuntimeError(
            f"No line geometries found for power={values} in {country}. "
            "The region may be too large for a single query — try without --single-query."
        )

    result = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    cache_set(key, result)
    return result


def power_tag_values(include_minor_lines: bool, include_cables: bool) -> list[str]:
    values = ["line"]
    if include_minor_lines:
        values.append("minor_line")
    if include_cables:
        values.append("cable")
    return values


def make_query_tiles(
    boundary: gpd.GeoDataFrame,
    tile_size_km: float,
    render_crs: str,
    sea_buffer_km: float = 0.0,
) -> gpd.GeoDataFrame:
    """Split a large country boundary into smaller projected tiles for Overpass."""
    if tile_size_km <= 0:
        raise ValueError("tile_size_km must be greater than zero")

    boundary_projected = boundary.to_crs(render_crs)
    country_geom = unary_union(boundary_projected.geometry)
    if not isinstance(country_geom, (Polygon, MultiPolygon)):
        raise RuntimeError("Boundary geometry is not polygonal")

    if sea_buffer_km > 0:
        # Inflate the land polygon by a sea margin so tiles cover water between
        # islands and short stretches of coast. Without this, power=cable ways
        # on the seabed (inter-island and cross-border interconnectors) are
        # never fetched from Overpass.
        country_geom = country_geom.buffer(sea_buffer_km * 1000)

    minx, miny, maxx, maxy = country_geom.bounds
    tile_size_m = tile_size_km * 1000
    tiles = []

    x_steps = np.arange(minx, maxx, tile_size_m)
    y_steps = np.arange(miny, maxy, tile_size_m)

    for x0 in x_steps:
        for y0 in y_steps:
            candidate = box(x0, y0, min(x0 + tile_size_m, maxx), min(y0 + tile_size_m, maxy))
            if not candidate.intersects(country_geom):
                continue
            clipped = candidate.intersection(country_geom)
            if not clipped.is_empty:
                tiles.append(clipped)

    if not tiles:
        raise RuntimeError("Could not create query tiles from the country boundary")

    return gpd.GeoDataFrame(geometry=tiles, crs=render_crs).to_crs("EPSG:4326")


# OSM element-identity columns kept per tile so cross-tile duplicates (ways
# spanning a tile border are returned by both tiles) can be dropped on merge.
_TILE_ID_COLS = ["element", "element_type", "osmid", "id"]

# Tag columns kept in the final combined frame, per feature kind.
_LINE_COLS = ["power", "voltage", "name", "operator", "geometry"]
_PLANT_COLS = ["power", "plant:source", "plant:output:electricity", "name", "operator", "geometry"]


def _osmnx_tile_fetcher(
    tags: dict[str, Any],
    geometry_types: list[str],
    keep_cols: list[str],
) -> Callable[[Any], gpd.GeoDataFrame]:
    """Build a ``fetch_fn`` that downloads ``tags`` via OSMnx for one tile.

    Returns a trimmed WGS84 GeoDataFrame (empty when the tile has no matching
    features). Used by the metadata-free line/plant paths.
    """

    def fetch_fn(tile_geom) -> gpd.GeoDataFrame:
        features = ox.features_from_polygon(tile_geom, tags=tags)
        if features.empty:
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        features = features.reset_index()
        matching = features[features.geometry.type.isin(geometry_types)]
        if matching.empty:
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        cols = [col for col in keep_cols if col in matching.columns]
        return gpd.GeoDataFrame(matching[cols], geometry="geometry", crs="EPSG:4326")

    return fetch_fn


def _fetch_tiles(
    tiles: gpd.GeoDataFrame,
    fetch_fn: Callable[[Any], gpd.GeoDataFrame],
    tile_cache_key: Callable[[Any], str],
    use_cache: bool,
    tile_delay: float,
) -> list[gpd.GeoDataFrame]:
    """Download features for every tile via ``fetch_fn``, returning one frame per tile.

    Shared engine behind every tiled fetch (OSMnx and raw-meta alike): per-tile
    caching, adaptive rate-limit backoff, and indefinite retries for failed tiles.
    ``fetch_fn(tile_geom)`` must return an already-filtered, trimmed WGS84
    GeoDataFrame (empty when the tile holds no matching features) and raise on
    transport/server errors so backoff can kick in.
    """
    frames: list[gpd.GeoDataFrame] = []
    empty_tile = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    rate_limit_delay = tile_delay

    def process_tile(tile_number: int, tile_geom, total: int) -> bool:
        """Fetch a tile's features and append to ``frames``. Returns True on success."""
        nonlocal rate_limit_delay
        if rate_limit_delay > 0:
            label = "Tile delay" if rate_limit_delay <= tile_delay else "Rate-limit backoff"
            print(f"  {label}: waiting {rate_limit_delay}s before next request")
            time.sleep(rate_limit_delay)
        try:
            tile_gdf = fetch_fn(tile_geom)
        except Exception as exc:
            # OSMnx raises this when Overpass returned a valid response with zero
            # matching features — not a server error, so cache as empty and move on.
            if "No matching features" in str(exc):
                cache_set(tile_cache_key(tile_geom), empty_tile)
                rate_limit_delay = max(tile_delay, rate_limit_delay - 5)
                return True
            is_rate_limit = "111" in str(exc) or "rate" in str(exc).lower() or "too many" in str(exc).lower()
            if is_rate_limit:
                rate_limit_delay = min(120, rate_limit_delay + 10)
            print(f"  Warning: tile {tile_number:,}/{total:,} failed: {exc}")
            return False
        rate_limit_delay = max(tile_delay, rate_limit_delay - 5)

        if tile_gdf is None or tile_gdf.empty:
            cache_set(tile_cache_key(tile_geom), empty_tile)
            return True

        cache_set(tile_cache_key(tile_geom), tile_gdf)
        frames.append(tile_gdf)
        return True

    total_tiles = len(tiles)
    uncached: list[tuple[int, Any]] = []
    cached_hits = 0
    for tile_number, tile_geom in enumerate(tiles.geometry, start=1):
        if use_cache:
            cached_tile = cache_get(tile_cache_key(tile_geom))
            if cached_tile is not None:
                if not cached_tile.empty:
                    frames.append(cached_tile)
                cached_hits += 1
                continue
        uncached.append((tile_number, tile_geom))

    if cached_hits:
        print(f"  Reused {cached_hits:,}/{total_tiles:,} tile(s) from per-tile cache")

    pending: list[tuple[int, Any]] = []
    for tile_number, tile_geom in uncached:
        print(f"  Tile {tile_number:,}/{total_tiles:,}")
        if not process_tile(tile_number, tile_geom, total_tiles):
            pending.append((tile_number, tile_geom))

    attempt = 1
    while pending:
        delay = min(300, max(rate_limit_delay, 10 * attempt))
        print(
            f"Retrying {len(pending):,} failed tile(s) in {delay}s "
            f"(attempt {attempt + 1})..."
        )
        time.sleep(delay)
        next_pending: list[tuple[int, Any]] = []
        for tile_number, tile_geom in pending:
            print(f"  Retry tile {tile_number:,}/{total_tiles:,}")
            if not process_tile(tile_number, tile_geom, total_tiles):
                next_pending.append((tile_number, tile_geom))

        if next_pending and len(next_pending) == len(pending):
            print(
                "  No tiles succeeded this round — Overpass may be returning "
                "the same error for these tiles; will keep retrying."
            )

        pending = next_pending
        attempt += 1

    return frames


def _combine_tile_frames(frames: list[gpd.GeoDataFrame], keep_cols: list[str]) -> gpd.GeoDataFrame:
    """Merge per-tile frames, drop cross-tile duplicates, and trim to ``keep_cols``."""
    combined = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry", crs="EPSG:4326")
    # Prefer OSM element identity for dedup. The meta path carries (osmid,
    # element_type, version), which uniquely identifies a feature even across
    # tile borders; the OSMnx path carries the element/osmid/id columns.
    if {"osmid", "element_type"}.issubset(combined.columns):
        dedup_cols = ["osmid", "element_type"]
        if "version" in combined.columns:
            dedup_cols.append("version")
        combined = combined.drop_duplicates(subset=dedup_cols)
    else:
        id_cols = [col for col in _TILE_ID_COLS if col in combined.columns]
        if id_cols:
            combined = combined.drop_duplicates(subset=id_cols)
        else:
            combined = combined.drop_duplicates(subset=["geometry"])
    return combined[[col for col in keep_cols if col in combined.columns]]


def fetch_power_features(
    country: str,
    boundary: gpd.GeoDataFrame,
    include_minor_lines: bool = False,
    include_cables: bool = False,
    tile_size_km: float = 200,
    render_crs: str = "EPSG:8857",
    sea_buffer_km: float = 0.0,
    use_cache: bool = True,
    tile_delay: float = 0,
) -> gpd.GeoDataFrame:
    values = power_tag_values(include_minor_lines, include_cables)
    key = cache_key("power_features", country, values, tile_size_km, render_crs, sea_buffer_km)
    if use_cache:
        cached = cache_get(key)
        if cached is not None:
            print(f"Using cached power features for {country}")
            return cached

    tiles = make_query_tiles(
        boundary,
        tile_size_km=tile_size_km,
        render_crs=render_crs,
        sea_buffer_km=sea_buffer_km,
    )
    print(f"Downloading OSM power features: power={values} across {len(tiles):,} tiles")

    def tile_cache_key(tile_geom: Any) -> str:
        # Per-tile key so partial progress survives a crash or Overpass outage:
        # geometry WKB folds in tile_size_km / render_crs / sea_buffer_km, since
        # those parameters fully determine the tile polygon.
        return cache_key("power_tile_v1", country, values, tile_geom.wkb_hex)

    frames = _fetch_tiles(
        tiles,
        fetch_fn=_osmnx_tile_fetcher(
            tags={"power": values},
            geometry_types=["LineString", "MultiLineString"],
            keep_cols=_TILE_ID_COLS + _LINE_COLS,
        ),
        tile_cache_key=tile_cache_key,
        use_cache=use_cache,
        tile_delay=tile_delay,
    )

    if not frames:
        raise RuntimeError(
            f"No line geometries found for power={values} in {country}. "
            "Try a smaller --tile-size-km or rerun later if Overpass is busy."
        )

    combined = _combine_tile_frames(frames, _LINE_COLS)
    cache_set(key, combined)
    return combined


def fetch_power_plants(
    country: str,
    boundary: gpd.GeoDataFrame,
    tile_size_km: float = 200,
    render_crs: str = "EPSG:8857",
    use_cache: bool = True,
    tile_delay: float = 0,
) -> gpd.GeoDataFrame:
    """Fetch power=plant features inside the boundary, tiled like the lines.

    Plants are nodes or areas, so point and polygon geometries are kept. An
    empty result is returned (not raised) when a region has no mapped plants —
    the overlay simply stays empty.
    """
    # Distinct cache namespaces ("power_plants_v1"/"power_plant_tile_v1") keep
    # plant tiles from ever colliding with the line tile cache.
    key = cache_key("power_plants_v1", country, tile_size_km, render_crs)
    if use_cache:
        cached = cache_get(key)
        if cached is not None:
            print(f"Using cached power plants for {country}")
            return cached

    tiles = make_query_tiles(boundary, tile_size_km=tile_size_km, render_crs=render_crs)
    print(f"Downloading OSM power plants: power=plant across {len(tiles):,} tiles")

    def tile_cache_key(tile_geom: Any) -> str:
        return cache_key("power_plant_tile_v1", country, tile_geom.wkb_hex)

    frames = _fetch_tiles(
        tiles,
        fetch_fn=_osmnx_tile_fetcher(
            tags={"power": "plant"},
            geometry_types=["Point", "Polygon", "MultiPolygon"],
            keep_cols=_TILE_ID_COLS + _PLANT_COLS,
        ),
        tile_cache_key=tile_cache_key,
        use_cache=use_cache,
        tile_delay=tile_delay,
    )

    if not frames:
        # Unlike lines, a region without mapped plants is a valid poster.
        combined = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        cache_set(key, combined)
        return combined

    combined = _combine_tile_frames(frames, _PLANT_COLS)
    cache_set(key, combined)
    return combined


# --------------------------------------------------------------------------- #
# Metadata-aware fetch (highlight mode)
#
# The highlight feature needs each element's last-editor and changeset, which
# OSMnx strips. These fetchers hit Overpass directly with `out meta geom;` and
# parse the user/uid/changeset/timestamp/version fields. They mirror the tiled
# engine above but use distinct cache namespaces ("*_meta_*") so they never
# collide with the metadata-free caches.
# --------------------------------------------------------------------------- #

# OSM element-identity columns kept from the meta query. (osmid, element_type,
# version) uniquely identify a feature, so cross-tile duplicates collapse cleanly.
_META_ID_COLS = ["osmid", "element_type", "version"]
# Per-element contribution metadata used to decide what gets highlighted.
_META_COLS = ["user", "uid", "changeset", "timestamp"]
_LINE_META_COLS = _META_ID_COLS + ["power", "voltage", "name", "operator"] + _META_COLS + ["geometry"]
_PLANT_META_COLS = (
    _META_ID_COLS
    + ["power", "plant:source", "plant:output:electricity", "name", "operator"]
    + _META_COLS
    + ["geometry"]
)
_SUBSTATION_META_COLS = (
    _META_ID_COLS + ["power", "voltage", "name", "operator"] + _META_COLS + ["geometry"]
)


def _overpass_interpreter_url() -> str:
    overpass_url = ox.settings.overpass_url.rstrip("/")
    if not overpass_url.endswith("/interpreter"):
        overpass_url += "/interpreter"
    return overpass_url


def _build_meta_query(
    polygons: list[Polygon],
    power_regex: str,
    element_kinds: tuple[str, ...],
    timeout: int,
) -> str:
    """Build an `out meta geom;` Overpass query over one or more poly: clips.

    ``element_kinds`` selects which OSM primitives to ask for — way-only for
    lines, but node/way/relation for plants and substations, which can be mapped
    as any of the three.
    """
    clauses = []
    for poly in polygons:
        poly_str = _polygon_to_overpass_poly(poly)
        for kind in element_kinds:
            clauses.append(f'  {kind}["power"~"{power_regex}"](poly:"{poly_str}");')
    return (
        f"[out:json][timeout:{timeout}];\n"
        "(\n" + "\n".join(clauses) + "\n);\n"
        "out meta geom;\n"
    )


def _element_geometry(elem: dict, want_lines: bool):
    """Build a shapely geometry from an `out geom` element, or None when degenerate.

    Lines keep their LineString; areas mapped as closed ways become Polygons;
    nodes become Points; relations collapse to the centroid of their member
    coordinates (a marker location is all the plant/substation overlay needs).
    """
    etype = elem.get("type")
    if etype == "node":
        if "lon" in elem and "lat" in elem:
            return Point(elem["lon"], elem["lat"])
        return None
    if etype == "way":
        coords = [(pt["lon"], pt["lat"]) for pt in elem.get("geometry", [])]
        if len(coords) < 2:
            return None
        if want_lines:
            return LineString(coords)
        # Closed ring with enough points → polygon; otherwise an open way.
        if len(coords) >= 4 and coords[0] == coords[-1]:
            try:
                return Polygon(coords)
            except Exception:
                return LineString(coords)
        return LineString(coords)
    if etype == "relation":
        coords: list[tuple[float, float]] = []
        for member in elem.get("members", []):
            for pt in member.get("geometry", []) or []:
                if "lon" in pt and "lat" in pt:
                    coords.append((pt["lon"], pt["lat"]))
        if not coords:
            return None
        return MultiPoint(coords).representative_point()
    return None


def _parse_meta_elements(
    elements: list[dict], keep_cols: list[str], want_lines: bool
) -> gpd.GeoDataFrame:
    """Turn raw Overpass meta elements into a trimmed WGS84 GeoDataFrame."""
    rows = []
    for elem in elements:
        geom = _element_geometry(elem, want_lines=want_lines)
        if geom is None or geom.is_empty:
            continue
        tags = elem.get("tags", {})
        rows.append(
            {
                "osmid": elem.get("id"),
                "element_type": elem.get("type"),
                "version": elem.get("version"),
                "power": tags.get("power"),
                "voltage": tags.get("voltage"),
                "name": tags.get("name"),
                "operator": tags.get("operator"),
                "plant:source": tags.get("plant:source"),
                "plant:output:electricity": tags.get("plant:output:electricity"),
                "user": elem.get("user"),
                "uid": elem.get("uid"),
                "changeset": elem.get("changeset"),
                "timestamp": elem.get("timestamp"),
                "geometry": geom,
            }
        )
    if not rows:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    frame = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    return frame[[col for col in keep_cols if col in frame.columns]]


def _raw_meta_tile_fetcher(
    power_regex: str,
    element_kinds: tuple[str, ...],
    keep_cols: list[str],
    want_lines: bool,
    timeout: int,
) -> Callable[[Any], gpd.GeoDataFrame]:
    """Build a ``fetch_fn`` that runs a raw `out meta geom;` query for one tile."""
    import requests as http_requests

    def fetch_fn(tile_geom) -> gpd.GeoDataFrame:
        polygons = _simplify_boundary_for_overpass(tile_geom)
        if not polygons:
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        query = _build_meta_query(polygons, power_regex, element_kinds, timeout)
        response = http_requests.post(
            _overpass_interpreter_url(),
            data={"data": query},
            timeout=timeout + 30,
            headers={"User-Agent": "GridToPoster/1.0"},
        )
        if response.status_code in (429, 504):
            # Surface as a rate-limit error so _fetch_tiles backs off and retries.
            raise RuntimeError(f"Overpass rate limit (HTTP {response.status_code})")
        response.raise_for_status()
        elements = response.json().get("elements", [])
        return _parse_meta_elements(elements, keep_cols, want_lines=want_lines)

    return fetch_fn


def fetch_power_features_meta(
    country: str,
    boundary: gpd.GeoDataFrame,
    include_minor_lines: bool = False,
    include_cables: bool = False,
    tile_size_km: float = 200,
    render_crs: str = "EPSG:3857",
    sea_buffer_km: float = 0.0,
    use_cache: bool = True,
    tile_delay: float = 0,
    timeout: int = 180,
) -> gpd.GeoDataFrame:
    """Like fetch_power_features but carries per-element OSM metadata."""
    values = power_tag_values(include_minor_lines, include_cables)
    power_regex = "^(" + "|".join(values) + ")$"
    key = cache_key("power_meta_features_v1", country, values, tile_size_km, render_crs, sea_buffer_km)
    if use_cache:
        cached = cache_get(key)
        if cached is not None:
            print(f"Using cached power features (meta) for {country}")
            return cached

    tiles = make_query_tiles(
        boundary, tile_size_km=tile_size_km, render_crs=render_crs, sea_buffer_km=sea_buffer_km
    )
    print(f"Downloading OSM power features with metadata: power={values} across {len(tiles):,} tiles")

    def tile_cache_key(tile_geom: Any) -> str:
        return cache_key("power_meta_tile_v1", country, values, tile_geom.wkb_hex)

    frames = _fetch_tiles(
        tiles,
        fetch_fn=_raw_meta_tile_fetcher(
            power_regex=power_regex,
            element_kinds=("way",),
            keep_cols=_LINE_META_COLS,
            want_lines=True,
            timeout=timeout,
        ),
        tile_cache_key=tile_cache_key,
        use_cache=use_cache,
        tile_delay=tile_delay,
    )
    if not frames:
        raise RuntimeError(
            f"No line geometries found for power={values} in {country}. "
            "Try a smaller --tile-size-km or rerun later if Overpass is busy."
        )
    combined = _combine_tile_frames(frames, _LINE_META_COLS)
    cache_set(key, combined)
    return combined


def fetch_power_plants_meta(
    country: str,
    boundary: gpd.GeoDataFrame,
    tile_size_km: float = 200,
    render_crs: str = "EPSG:3857",
    use_cache: bool = True,
    tile_delay: float = 0,
    timeout: int = 180,
) -> gpd.GeoDataFrame:
    """Like fetch_power_plants but carries per-element OSM metadata."""
    key = cache_key("power_meta_plants_v1", country, tile_size_km, render_crs)
    if use_cache:
        cached = cache_get(key)
        if cached is not None:
            print(f"Using cached power plants (meta) for {country}")
            return cached

    tiles = make_query_tiles(boundary, tile_size_km=tile_size_km, render_crs=render_crs)
    print(f"Downloading OSM power plants with metadata: power=plant across {len(tiles):,} tiles")

    def tile_cache_key(tile_geom: Any) -> str:
        return cache_key("power_meta_plant_tile_v1", country, tile_geom.wkb_hex)

    frames = _fetch_tiles(
        tiles,
        fetch_fn=_raw_meta_tile_fetcher(
            power_regex="^plant$",
            element_kinds=("node", "way", "relation"),
            keep_cols=_PLANT_META_COLS,
            want_lines=False,
            timeout=timeout,
        ),
        tile_cache_key=tile_cache_key,
        use_cache=use_cache,
        tile_delay=tile_delay,
    )
    if not frames:
        combined = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        cache_set(key, combined)
        return combined
    combined = _combine_tile_frames(frames, _PLANT_META_COLS)
    cache_set(key, combined)
    return combined


def fetch_substations(
    country: str,
    boundary: gpd.GeoDataFrame,
    tile_size_km: float = 200,
    render_crs: str = "EPSG:3857",
    use_cache: bool = True,
    tile_delay: float = 0,
    timeout: int = 180,
) -> gpd.GeoDataFrame:
    """Fetch power=substation features (with metadata) inside the boundary.

    Substations are mapped as nodes, ways (areas) or relations, all collapsed to
    a marker point downstream. An empty result is returned (not raised) when a
    region has no mapped substations.
    """
    key = cache_key("substations_meta_v1", country, tile_size_km, render_crs)
    if use_cache:
        cached = cache_get(key)
        if cached is not None:
            print(f"Using cached substations for {country}")
            return cached

    tiles = make_query_tiles(boundary, tile_size_km=tile_size_km, render_crs=render_crs)
    print(f"Downloading OSM substations: power=substation across {len(tiles):,} tiles")

    def tile_cache_key(tile_geom: Any) -> str:
        return cache_key("substation_meta_tile_v1", country, tile_geom.wkb_hex)

    frames = _fetch_tiles(
        tiles,
        fetch_fn=_raw_meta_tile_fetcher(
            power_regex="^substation$",
            element_kinds=("node", "way", "relation"),
            keep_cols=_SUBSTATION_META_COLS,
            want_lines=False,
            timeout=timeout,
        ),
        tile_cache_key=tile_cache_key,
        use_cache=use_cache,
        tile_delay=tile_delay,
    )
    if not frames:
        combined = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        cache_set(key, combined)
        return combined
    combined = _combine_tile_frames(frames, _SUBSTATION_META_COLS)
    cache_set(key, combined)
    return combined


def assign_is_highlighted(
    frame: gpd.GeoDataFrame,
    highlight_users: set[str],
    highlight_changesets: set[int],
    since: str | None = None,
) -> pd.Series:
    """Per-row boolean: True when the element's editor or changeset is highlighted.

    Union semantics — a feature matches if its last editor is in ``highlight_users``
    OR its changeset is in ``highlight_changesets``. When ``since`` (an OSM-style
    ``YYYY-MM-DDTHH:MM:SSZ`` timestamp) is given, only features last edited on or
    after that instant can match — ISO 8601 timestamps sort lexicographically, so
    a plain string comparison is correct.
    """
    n = len(frame)
    if n == 0:
        return pd.Series([], dtype=bool)
    user_hit = (
        frame["user"].isin(highlight_users)
        if highlight_users and "user" in frame.columns
        else pd.Series(False, index=frame.index)
    )
    if highlight_changesets and "changeset" in frame.columns:
        cs = pd.to_numeric(frame["changeset"], errors="coerce")
        cs_hit = cs.isin(highlight_changesets)
    else:
        cs_hit = pd.Series(False, index=frame.index)
    match = (user_hit | cs_hit).fillna(False)
    if since and "timestamp" in frame.columns:
        recent = frame["timestamp"].fillna("").astype(str) >= since
        match = match & recent
    return match
