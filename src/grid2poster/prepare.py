"""OSM tag parsing (voltage, capacity, plant:source) and geometry preparation."""

from __future__ import annotations

import re
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.ops import unary_union

from .common import tqdm


def parse_voltage_to_kv(value: Any) -> float | None:
    """Parse OSM voltage tags into kV, using pragmatic cleanup for poster styling."""
    if value is None:
        return None
    if isinstance(value, float) and np.isnan(value):
        return None
    if isinstance(value, (list, tuple, set)):
        parsed = [parse_voltage_to_kv(item) for item in value]
        parsed = [item for item in parsed if item is not None]
        return max(parsed) if parsed else None

    text = str(value).lower().replace(" ", "")
    tokens = re.split(r"[;,/|]+", text)
    values: list[float] = []
    for token in tokens:
        if not token:
            continue
        multiplier = 1.0
        if token.endswith("kv"):
            multiplier = 1.0
            token = token[:-2]
        elif token.endswith("v"):
            multiplier = 0.001
            token = token[:-1]

        token = token.replace(",", ".")
        match = re.search(r"\d+(?:\.\d+)?", token)
        if not match:
            continue

        # OSM voltage is usually in volts, e.g. 380000; convert those to kV.
        number = float(match.group(0))
        number = number / 1000.0 if multiplier == 1.0 and number > 1200 else number * multiplier
        values.append(number)

    return max(values) if values else None


def parse_capacity_to_mw(value: Any) -> float:
    """Parse OSM plant:output:electricity tags into MW, NaN when unparseable.

    Multiple values (lists or ``"500 MW;200 MW"``) are summed because a plant
    tagged with several unit outputs produces their total — unlike voltage,
    where the max is the line's rating.
    """
    if value is None:
        return float("nan")
    if isinstance(value, float) and np.isnan(value):
        return float("nan")
    if isinstance(value, (list, tuple, set)):
        parsed = [parse_capacity_to_mw(item) for item in value]
        parsed = [item for item in parsed if not np.isnan(item)]
        return float(sum(parsed)) if parsed else float("nan")

    # ";" is OSM's multi-value separator; "," is kept for European decimals
    # ("1,5 MW") and converted to "." per token below.
    text = str(value).lower().replace(" ", "")
    tokens = text.split(";")
    values: list[float] = []
    for token in tokens:
        if not token:
            continue
        multiplier = 1.0
        if token.endswith("gw"):
            multiplier = 1000.0
            token = token[:-2]
        elif token.endswith("mw"):
            token = token[:-2]
        elif token.endswith("kw"):
            multiplier = 0.001
            token = token[:-2]
        elif token.endswith("w"):
            multiplier = 1e-6
            token = token[:-1]

        token = token.replace(",", ".")
        match = re.search(r"\d+(?:\.\d+)?", token)
        if not match:
            # Tags like "yes" carry no numeric output; skip them.
            continue
        values.append(float(match.group(0)) * multiplier)

    return float(sum(values)) if values else float("nan")


# Plant:source values are bucketed into these categories for marker coloring;
# the order also fixes the plant legend row ordering.
PLANT_SOURCE_BUCKETS: tuple[str, ...] = (
    "solar",
    "wind",
    "hydro",
    "nuclear",
    "coal",
    "gas",
    "oil",
    "biomass",
    "other",
)

# Substring → bucket, checked in order so e.g. "biogas" matches biomass before
# the bare "gas" keyword. Rare sources fold into the nearest bucket (tidal →
# hydro, waste → biomass); the rest (geothermal, battery, ...) become "other".
_PLANT_SOURCE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("photovoltaic", "solar"),
    ("solar", "solar"),
    ("pv", "solar"),
    ("wind", "wind"),
    ("hydro", "hydro"),
    ("tidal", "hydro"),
    ("wave", "hydro"),
    ("water", "hydro"),
    ("nuclear", "nuclear"),
    ("lignite", "coal"),
    ("coal", "coal"),
    ("biomass", "biomass"),
    ("biogas", "biomass"),
    ("biofuel", "biomass"),
    ("wood", "biomass"),
    ("waste", "biomass"),
    ("gas", "gas"),
    ("diesel", "oil"),
    ("petroleum", "oil"),
    ("oil", "oil"),
)


def bucket_plant_source(source: Any) -> str:
    """Map an OSM plant:source tag onto one of PLANT_SOURCE_BUCKETS."""
    if source is None:
        return "other"
    if isinstance(source, float) and np.isnan(source):
        return "other"
    if isinstance(source, (list, tuple, set)):
        for item in source:
            bucket = bucket_plant_source(item)
            if bucket != "other":
                return bucket
        return "other"

    text = str(source).lower()
    # Multi-source plants ("gas;oil") are colored by their first recognizable
    # source — usually the dominant one by tagging convention.
    for token in re.split(r"[;,/|]+", text):
        token = token.strip()
        if not token:
            continue
        for keyword, bucket in _PLANT_SOURCE_KEYWORDS:
            if keyword in token:
                return bucket
    return "other"


def prepare_lines(
    lines: gpd.GeoDataFrame,
    boundary: gpd.GeoDataFrame,
    output_crs: str,
    cable_sea_buffer_km: float = 0.0,
) -> gpd.GeoDataFrame:
    # These vectorized geometry ops (reprojection, clipping, exploding) are the
    # heaviest part of data prep before plotting and can take a while on dense
    # frames, so step a progress bar through each stage.
    with tqdm(total=5, desc="Preparing lines", unit="step", leave=True) as bar:
        bar.set_description("Reprojecting")
        boundary_projected = boundary.to_crs(output_crs)
        lines_projected = lines.to_crs(output_crs)
        bar.update()

        if "power" in lines_projected.columns and cable_sea_buffer_km > 0:
            is_cable = lines_projected["power"] == "cable"
        else:
            is_cable = pd.Series(False, index=lines_projected.index)

        def _safe_clip(frame: gpd.GeoDataFrame, mask_geom) -> gpd.GeoDataFrame:
            # Power grids lie overwhelmingly inside the boundary, so running a
            # geometric intersection on every line (as gpd.clip does) wastes
            # work on the ~95-99% that never cross it. Keep fully-contained
            # lines untouched and intersect only the crossing remainder —
            # roughly an order of magnitude faster on dense countries.
            try:
                geoms = frame.geometry.values
                shapely.prepare(mask_geom)
                crossing = ~shapely.contains_properly(mask_geom, geoms)
                if crossing.any():
                    new_geoms = geoms.copy()
                    new_geoms[crossing] = shapely.intersection(geoms[crossing], mask_geom)
                    frame = frame.copy()
                    frame["geometry"] = new_geoms
                return frame[~shapely.is_empty(frame.geometry.values)]
            except Exception:
                # Clipping may fail with invalid upstream geometries. A poster can
                # still be rendered without clipping because the Overpass polygon
                # query already constrained the result set.
                return frame

        bar.set_description("Clipping")
        parts: list[gpd.GeoDataFrame] = []
        land_lines = lines_projected[~is_cable]
        if not land_lines.empty:
            land_mask = unary_union(boundary_projected.geometry)
            parts.append(_safe_clip(land_lines, land_mask))
        cable_lines = lines_projected[is_cable]
        if not cable_lines.empty:
            cable_mask = unary_union(boundary_projected.geometry.buffer(cable_sea_buffer_km * 1000))
            parts.append(_safe_clip(cable_lines, cable_mask))
        bar.update()

        clipped = gpd.GeoDataFrame(
            pd.concat(parts, ignore_index=True) if parts else lines_projected.iloc[0:0],
            geometry="geometry",
            crs=output_crs,
        )

        bar.set_description("Exploding")
        clipped = clipped.explode(ignore_index=True)
        clipped = clipped[clipped.geometry.type.isin(["LineString", "MultiLineString"])]
        clipped = clipped[~clipped.geometry.is_empty]
        if clipped.empty:
            raise RuntimeError("Power-line geometries became empty after projection/clipping")
        bar.update()

        bar.set_description("Parsing voltages")
        clipped["voltage_kv"] = clipped.get("voltage", None).apply(parse_voltage_to_kv)
        clipped["sort_voltage"] = clipped["voltage_kv"].fillna(0)
        bar.update()

        bar.set_description("Sorting")
        result = clipped.sort_values("sort_voltage")
        bar.update()

    return result


def prepare_plants(
    plants: gpd.GeoDataFrame,
    boundary: gpd.GeoDataFrame,
    output_crs: str,
    min_capacity_mw: float = 0.0,
) -> gpd.GeoDataFrame:
    """Project plants, reduce them to marker points, clip, and parse tags."""
    if plants.empty:
        return plants

    plants_projected = plants.to_crs(output_crs).copy()
    boundary_projected = boundary.to_crs(output_crs)

    # Plants are mapped as nodes or areas; representative_point() collapses
    # both to a marker location guaranteed inside the plant footprint.
    plants_projected["geometry"] = plants_projected.geometry.representative_point()

    mask_geom = unary_union(boundary_projected.geometry)
    shapely.prepare(mask_geom)
    inside = shapely.contains(mask_geom, plants_projected.geometry.values)
    plants_projected = plants_projected[inside]

    capacity_raw = plants_projected.get(
        "plant:output:electricity", pd.Series(index=plants_projected.index, dtype=object)
    )
    plants_projected["capacity_mw"] = capacity_raw.apply(parse_capacity_to_mw)
    source_raw = plants_projected.get(
        "plant:source", pd.Series(index=plants_projected.index, dtype=object)
    )
    plants_projected["source_bucket"] = source_raw.apply(bucket_plant_source)

    if min_capacity_mw > 0:
        # An explicit threshold is a de-clutter request, so unknown-capacity
        # plants are dropped too rather than slipping through as fallback dots.
        plants_projected = plants_projected[plants_projected["capacity_mw"] >= min_capacity_mw]

    return plants_projected
