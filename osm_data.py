"""Boundary resolution and Overpass downloads of OSM power features."""

from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import geopandas as gpd
import numpy as np
import osmnx as ox
import pandas as pd
from shapely.geometry import LineString, MultiPolygon, Polygon, box
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


@contextmanager
def _historical_overpass_settings(historical_date: str | None):
    """Temporarily inject an Overpass ``[date:"..."]`` attic-data filter.

    osmnx builds every query via ``settings.overpass_settings.format(timeout=...,
    maxsize=...)`` (see ``osmnx._overpass._make_overpass_settings``), so adding
    the date filter to that template is the only hook available to make
    ``ox.features_from_polygon`` return point-in-time data instead of current
    data. Restores the original template on exit so unrelated (current-day)
    fetches are unaffected.
    """
    if not historical_date:
        yield
        return
    original = ox.settings.overpass_settings
    ox.settings.overpass_settings = (
        f'[out:json][timeout:{{timeout}}][date:"{historical_date}"]{{maxsize}}'
    )
    try:
        yield
    finally:
        ox.settings.overpass_settings = original


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
    historical_date: str | None = None,
) -> gpd.GeoDataFrame:
    """Fetch all power features in one Overpass query using poly: filter.

    ``historical_date`` (ISO 8601, e.g. ``"2020-01-01T00:00:00Z"``) requests
    attic (point-in-time) data instead of the current OSM state. This path has
    no retry loop, so prefer the tiled ``fetch_power_features`` for historical
    queries over large areas, where attic queries are more timeout-prone.
    """
    import requests as http_requests

    values = power_tag_values(include_minor_lines, include_cables)
    if historical_date:
        key = cache_key("power_single_historical_v1", country, values, sea_buffer_km, historical_date)
    else:
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

    date_clause = f'[date:"{historical_date}"]' if historical_date else ""
    query = (
        f"[out:json][timeout:{timeout}]{date_clause};\n"
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


def _fetch_tiles(
    tiles: gpd.GeoDataFrame,
    tags: dict[str, Any],
    tile_cache_key: Callable[[Any], str],
    geometry_types: list[str],
    keep_cols: list[str],
    use_cache: bool,
    tile_delay: float,
) -> list[gpd.GeoDataFrame]:
    """Download ``tags`` features for every tile, returning one frame per tile.

    Shared engine behind fetch_power_features and fetch_power_plants: per-tile
    caching, adaptive rate-limit backoff, and indefinite retries for failed
    tiles. Only features whose geometry type is in ``geometry_types`` are kept,
    trimmed to ``keep_cols``.
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
            features = ox.features_from_polygon(tile_geom, tags=tags)
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

        if features.empty:
            cache_set(tile_cache_key(tile_geom), empty_tile)
            return True

        features = features.reset_index()
        matching = features[features.geometry.type.isin(geometry_types)]
        if matching.empty:
            cache_set(tile_cache_key(tile_geom), empty_tile)
            return True

        cols = [col for col in keep_cols if col in matching.columns]
        tile_gdf = gpd.GeoDataFrame(matching[cols], geometry="geometry", crs="EPSG:4326")
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
    historical_date: str | None = None,
) -> gpd.GeoDataFrame:
    """``historical_date`` (ISO 8601, e.g. ``"2020-01-01T00:00:00Z"``) requests
    attic (point-in-time) data instead of the current OSM state. Attic queries
    are more timeout-prone on the public Overpass instance than current-data
    queries; the per-tile retry/backoff below absorbs that.
    """
    values = power_tag_values(include_minor_lines, include_cables)
    if historical_date:
        key = cache_key(
            "power_features_historical_v1", country, values, tile_size_km, render_crs, sea_buffer_km, historical_date
        )
    else:
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
        if historical_date:
            return cache_key("power_tile_historical_v1", country, values, tile_geom.wkb_hex, historical_date)
        return cache_key("power_tile_v1", country, values, tile_geom.wkb_hex)

    with _historical_overpass_settings(historical_date):
        frames = _fetch_tiles(
            tiles,
            tags={"power": values},
            tile_cache_key=tile_cache_key,
            geometry_types=["LineString", "MultiLineString"],
            keep_cols=_TILE_ID_COLS + _LINE_COLS,
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
    historical_date: str | None = None,
) -> gpd.GeoDataFrame:
    """Fetch power=plant features inside the boundary, tiled like the lines.

    Plants are nodes or areas, so point and polygon geometries are kept. An
    empty result is returned (not raised) when a region has no mapped plants —
    the overlay simply stays empty. ``historical_date`` (ISO 8601) requests
    attic (point-in-time) data instead of the current OSM state.
    """
    # Distinct cache namespaces ("power_plants_v1"/"power_plant_tile_v1") keep
    # plant tiles from ever colliding with the line tile cache.
    if historical_date:
        key = cache_key("power_plants_historical_v1", country, tile_size_km, render_crs, historical_date)
    else:
        key = cache_key("power_plants_v1", country, tile_size_km, render_crs)
    if use_cache:
        cached = cache_get(key)
        if cached is not None:
            print(f"Using cached power plants for {country}")
            return cached

    tiles = make_query_tiles(boundary, tile_size_km=tile_size_km, render_crs=render_crs)
    print(f"Downloading OSM power plants: power=plant across {len(tiles):,} tiles")

    def tile_cache_key(tile_geom: Any) -> str:
        if historical_date:
            return cache_key("power_plant_tile_historical_v1", country, tile_geom.wkb_hex, historical_date)
        return cache_key("power_plant_tile_v1", country, tile_geom.wkb_hex)

    with _historical_overpass_settings(historical_date):
        frames = _fetch_tiles(
            tiles,
            tags={"power": "plant"},
            tile_cache_key=tile_cache_key,
            geometry_types=["Point", "Polygon", "MultiPolygon"],
            keep_cols=_TILE_ID_COLS + _PLANT_COLS,
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
