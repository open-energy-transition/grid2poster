#!/usr/bin/env python3
"""
GridToPoster
============
Create beautiful country-level electrical transmission grid posters from
OpenStreetMap `power=line` features.

This is a focused rewrite of a city street-network poster workflow: instead of
fetching roads around a city point, it geocodes a country boundary, downloads
OpenStreetMap power-line geometries inside that boundary, styles them by voltage,
and renders a print-ready poster.

This module is the CLI entry point; the pipeline stages live in sibling modules:

- ``common``   - shared constants, the on-disk cache, small utilities
- ``osm_data`` - boundary resolution and Overpass downloads
- ``prepare``  - OSM tag parsing and geometry preparation
- ``theming``  - themes and per-feature styling
- ``render``   - poster composition and file output
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

import osmnx as ox

from common import (
    DEFAULT_VOLTAGE_TIERS,
    MM_PER_INCH,
    PAPER_SIZES,
    POSTERS_DIR,
    slugify,
)
from changesets import resolve_highlight_changesets
from osm_data import (
    assign_is_highlighted,
    fetch_power_features,
    fetch_power_features_meta,
    fetch_power_features_single,
    fetch_power_plants,
    fetch_power_plants_meta,
    fetch_substations,
    get_country_boundary,
    load_boundary_from_geojson,
)
from prepare import prepare_lines, prepare_plants, prepare_substations
from render import load_logo_image, render_poster
from theming import list_themes, load_theme


def output_path(country: str, theme_id: str, fmt: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return POSTERS_DIR / f"{slugify(country)}_grid_{theme_id}_{timestamp}.{fmt}"


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create country electrical transmission grid posters from OpenStreetMap power=line data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--country", "-C", required=False, help="Country or region name resolvable by Nominatim")
    parser.add_argument(
        "--boundary-geojson",
        type=Path,
        help="Load the boundary polygon(s) from a local GeoJSON file instead of geocoding via Nominatim. "
             "All polygonal features in the file are dissolved into a single boundary.",
    )
    parser.add_argument(
        "--internal-borders",
        action="store_true",
        help="When loading --boundary-geojson, keep each feature as a separate part so borders "
             "shared between adjacent features (e.g. provinces) are drawn instead of dissolved away.",
    )
    parser.add_argument("--display-country", help="Text to print on the poster")
    parser.add_argument(
        "--subtitle",
        help="Override the poster subtitle (default: 'ELECTRICAL TRANSMISSION GRID', "
             "or 'ELECTRICAL GRID' with --include-minor-lines)",
    )
    parser.add_argument(
        "--padding",
        type=float,
        default=0.10,
        help="Fractional padding around the boundary bounds. Lower = more zoomed in "
             "(e.g. 0 = tight fit, -0.05 = crop slightly into the bounds, 0.20 = looser).",
    )
    parser.add_argument(
        "--shift-x",
        type=float,
        default=0.0,
        help="Shift the grid data horizontally on the poster, as a fraction of the "
             "data extent. Positive values shift right, negative shift left "
             "(e.g. 0.1 = shift 10%% right).",
    )
    parser.add_argument(
        "--shift-y",
        type=float,
        default=0.0,
        help="Shift the grid data vertically on the poster, as a fraction of the "
             "data extent. Positive values shift up, negative shift down "
             "(e.g. 0.1 = shift 10%% up).",
    )
    parser.add_argument("--theme", "-t", default="paper_grid", help="Theme ID from themes/")
    parser.add_argument("--list-themes", action="store_true", help="List available themes and exit")
    parser.add_argument(
        "--voltage-tiers",
        type=parse_voltage_tiers,
        default=DEFAULT_VOLTAGE_TIERS,
        metavar="LOW,MID,HIGH,EXTRA",
        help="Lower kV bounds for the four voltage tiers, comma-separated "
             "(default: 60,150,300,500). Sets how lines are colored/weighted and "
             "the legend labels; tune to the grid being mapped.",
    )
    parser.add_argument("--include-minor-lines", action="store_true", help="Also fetch power=minor_line")
    parser.add_argument(
        "--include-cables",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Fetch power=cable features (underground/submarine). Off by default; pass --include-cables to enable.",
    )
    parser.add_argument(
        "--cable-sea-buffer-km",
        type=float,
        default=200.0,
        help="When --include-cables is on, inflate the boundary by this many kilometers "
             "over water so submarine cables between islands and to neighboring countries "
             "are queried from Overpass and survive coastline clipping. Set to 0 to disable.",
    )
    parser.add_argument(
        "--show-plants",
        action="store_true",
        help="Fetch power=plant features and overlay them as markers sized by "
             "capacity (plant:output:electricity) and colored by source (plant:source).",
    )
    parser.add_argument(
        "--min-plant-capacity",
        type=float,
        default=0.0,
        metavar="MW",
        help="Only draw plants with at least this electrical output in MW. Plants "
             "with unknown capacity are dropped when set. Default 0 (show all).",
    )
    parser.add_argument(
        "--plant-marker-scale",
        type=float,
        default=1.0,
        help="Multiplier for plant marker sizes (default 1.0). Increase for sparse "
             "grids, decrease to reduce clutter.",
    )
    parser.add_argument(
        "--highlight-hashtag",
        default=None,
        metavar="TAG",
        help="Highlight power features whose OSM changeset comment/hashtags contain "
             "this hashtag (case-insensitive, leading '#' optional, e.g. MapYourGrid). "
             "Enables highlight mode: matched lines are drawn vividly over a dimmed "
             "base, and power plants + substations are auto-fetched and highlighted.",
    )
    parser.add_argument(
        "--highlight-users",
        type=parse_user_list,
        default=set(),
        metavar="USER1,USER2",
        help="Highlight power features last-edited by any of these OSM usernames "
             "(comma-separated, case-sensitive). Enables highlight mode. Combined "
             "with --highlight-hashtag by union (a feature matching EITHER is highlighted).",
    )
    parser.add_argument(
        "--highlight-since",
        type=parse_highlight_since,
        default=None,
        metavar="DATE",
        help="Only highlight features last edited on or after this date "
             "(YYYY-MM-DD or full ISO8601, treated as UTC). Features edited earlier "
             "are never highlighted and their changesets are skipped, which also cuts "
             "OSM changeset API lookups for --highlight-hashtag. E.g. 2024-12-01.",
    )
    parser.add_argument(
        "--highlight-request-delay",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="Seconds to wait between OSM changeset API batch requests when resolving "
             "--highlight-hashtag (default: 1.0). Raise to stay polite on large regions.",
    )
    parser.add_argument(
        "--include-outlying",
        action="store_true",
        help="Keep overseas territories and other polygons far from the main landmass. "
             "By default only the mainland (and nearby islands) is rendered.",
    )
    parser.add_argument(
        "--paper-size",
        choices=sorted(PAPER_SIZES),
        help="Preset paper size in portrait orientation. Overrides --width and --height. "
             "Use --landscape to flip orientation.",
    )
    parser.add_argument("--width", "-W", type=float, default=297.0, help="Poster width in millimeters (default: A3 short side)")
    parser.add_argument("--height", "-H", type=float, default=420.0, help="Poster height in millimeters (default: A3 long side)")
    parser.add_argument(
        "--landscape",
        action="store_true",
        help="Render in landscape (horizontal) orientation. Swaps width and height if width < height.",
    )
    parser.add_argument("--dpi", type=int, default=300, help="Raster output DPI")
    parser.add_argument(
        "--title-size",
        type=float,
        default=None,
        help="Title font size in points. Defaults to an auto-scaled value based on poster size.",
    )
    parser.add_argument(
        "--tile-size-km",
        type=float,
        default=400,
        help="Overpass query tile size in kilometers. Use smaller values for very large countries or busy servers.",
    )
    parser.add_argument(
        "--format",
        "-f",
        nargs="+",
        choices=["png", "svg", "pdf"],
        default=["png", "svg"],
        help="Output format(s). Pass multiple values to write the poster in several formats at once.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output file path. When set, only a single file is written and its format is inferred from the extension.",
    )
    parser.add_argument(
        "--crs",
        default="EPSG:3857",
        help="Projection used for rendering. EPSG:3857 Pseudo-Mercator works well for country posters.",
    )
    parser.add_argument("--hide-metadata", action="store_true", help="Do not print segment counts on poster")
    parser.add_argument("--hide-borders", action="store_true", help="Do not draw the region boundary outline")
    parser.add_argument(
        "--transparent-background",
        action="store_true",
        help="Render with a transparent background instead of the theme background color. "
             "The grid, text, and fades are kept; only the backdrop is made transparent. "
             "Best used with PNG or SVG output (PDF transparency support is limited).",
    )
    parser.add_argument(
        "--fade-top-height",
        type=float,
        default=0.28,
        help="Fraction of the poster height covered by the top fade-to-background "
             "gradient (default: 0.28). Lower = shorter fade; 0 disables it.",
    )
    parser.add_argument(
        "--fade-top-alpha",
        type=float,
        default=1.0,
        help="Opacity of the top fade at the poster edge, 0 (none) to 1 (fully "
             "opaque, default). Lower = lighter fade.",
    )
    parser.add_argument(
        "--fade-bottom-height",
        type=float,
        default=0.28,
        help="Fraction of the poster height covered by the bottom fade gradient "
             "(default: 0.28). Lower = shorter fade; 0 disables it.",
    )
    parser.add_argument(
        "--fade-bottom-alpha",
        type=float,
        default=1.0,
        help="Opacity of the bottom fade at the poster edge, 0 (none) to 1 (fully "
             "opaque, default). Lower = lighter fade.",
    )
    parser.add_argument(
        "--logo",
        type=Path,
        default=None,
        help="Path to an SVG or PNG logo to place in the lower-left corner.",
    )
    parser.add_argument(
        "--logo-size",
        type=float,
        default=20.0,
        help="Logo width in millimeters (height scales to preserve the aspect ratio).",
    )
    parser.add_argument(
        "--logo-margin",
        type=float,
        default=12.0,
        help="Margin in millimeters between the logo and the lower-left poster edges.",
    )
    parser.add_argument(
        "--logo-alpha",
        type=float,
        default=1.0,
        help="Logo opacity from 0 (transparent) to 1 (fully opaque).",
    )
    parser.add_argument(
        "--export-geojson",
        nargs="?",
        const="",
        default=None,
        help="Also save all transmission lines as a single GeoJSON (WGS84). "
             "Optionally pass a path; otherwise written next to the poster.",
    )
    parser.add_argument(
        "--single-query",
        action="store_true",
        help="Fetch all power features in a single Overpass query instead of tiling. "
             "Faster for small/medium regions but may time out on large countries or continents.",
    )
    parser.add_argument(
        "--tile-delay",
        type=float,
        default=30,
        help="Seconds to wait between Overpass tile API requests (default: 30). "
             "Useful to avoid rate-limiting on busy public endpoints.",
    )
    parser.add_argument("--verbose-osmnx", action="store_true", help="Print OSMnx request logs")
    parser.add_argument(
        "--overpass-endpoint",
        help="Override the Overpass API endpoint. Use a mirror when the default "
             "(overpass-api.de) is rate-limiting or refusing connections. "
             "Examples: https://overpass.kumi.systems/api/interpreter, "
             "https://overpass.private.coffee/api/interpreter, "
             "https://overpass.osm.ch/api/interpreter.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore cached boundaries and OSM power features on this run. "
             "Fresh results are still written to the cache for future runs.",
    )
    return parser.parse_args(list(argv))


def parse_user_list(value: str) -> set[str]:
    """Parse a comma-separated list of OSM usernames into a set (whitespace-trimmed)."""
    return {name.strip() for name in value.split(",") if name.strip()}


def parse_highlight_since(value: str) -> str:
    """Normalize a YYYY-MM-DD (or full ISO8601) cutoff into an OSM-style UTC timestamp.

    OSM `out meta` timestamps look like ``2024-12-01T00:00:00Z`` and sort
    lexicographically, so returning the same fixed-width format lets the highlight
    filter compare timestamps as plain strings.
    """
    from datetime import datetime, timezone

    text = value.strip()
    try:
        if "T" in text:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--highlight-since must be YYYY-MM-DD or ISO8601: {value!r}"
        )
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_voltage_tiers(value: str) -> tuple[float, float, float, float]:
    """Parse a 'low,mid,high,extra' kV string into a strictly-increasing tuple."""
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "expected four comma-separated kV values, e.g. 60,150,300,500"
        )
    try:
        tiers = tuple(float(p) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(f"voltage tiers must be numbers: {value!r}")
    if tiers[0] <= 0:
        raise argparse.ArgumentTypeError("voltage tiers must be positive")
    if any(a >= b for a, b in zip(tiers, tiers[1:])):
        raise argparse.ArgumentTypeError(
            f"voltage tiers must strictly increase: {value!r}"
        )
    return tiers  # type: ignore[return-value]


def main(argv: Iterable[str] = sys.argv[1:]) -> int:
    args = parse_args(argv)

    if args.list_themes:
        list_themes()
        return 0
    if not args.country:
        print("Error: --country is required unless --list-themes is used", file=sys.stderr)
        return 2

    ox.settings.use_cache = not args.no_cache
    ox.settings.log_console = bool(args.verbose_osmnx)
    ox.settings.requests_timeout = 180
    if args.overpass_endpoint:
        ox.settings.overpass_url = args.overpass_endpoint
        print(f"Using Overpass endpoint: {args.overpass_endpoint}")
    # Keep OSMnx's own guard reasonably high: we explicitly tile the country
    # boundary below, so this setting is only a secondary safety net.
    ox.settings.max_query_area_size = max(ox.settings.max_query_area_size, (args.tile_size_km * 1000) ** 2 * 2)

    theme = load_theme(args.theme)
    display_country = args.display_country or args.country

    logo_image = None
    if args.logo is not None:
        if not args.logo.exists():
            print(f"Error: logo file not found: {args.logo}", file=sys.stderr)
            return 2
        logo_image = load_logo_image(args.logo)

    if args.paper_size:
        width_mm, height_mm = PAPER_SIZES[args.paper_size]
    else:
        width_mm, height_mm = args.width, args.height
    if args.landscape and width_mm < height_mm:
        width_mm, height_mm = height_mm, width_mm
    width, height = width_mm / MM_PER_INCH, height_mm / MM_PER_INCH

    if args.boundary_geojson:
        print(f"Loading boundary from {args.boundary_geojson}")
        boundary_wgs84 = load_boundary_from_geojson(
            args.boundary_geojson, args.country, keep_internal_borders=args.internal_borders
        )
    else:
        boundary_wgs84 = get_country_boundary(
            args.country,
            mainland_only=not args.include_outlying,
            use_cache=not args.no_cache,
        )
    highlight_users = args.highlight_users
    highlight_hashtag = args.highlight_hashtag.lstrip("#").strip() if args.highlight_hashtag else None
    highlight_mode = bool(highlight_users) or bool(highlight_hashtag)
    highlight_label = None

    cable_buffer_km = args.cable_sea_buffer_km if args.include_cables else 0.0
    boundary_projected = boundary_wgs84.to_crs(args.crs)
    substations_projected = None

    if highlight_mode:
        # Highlight mode needs per-element metadata (user/changeset), which only
        # the tiled raw-Overpass path returns — so --single-query is ignored here.
        if args.single_query:
            print("Note: --single-query is ignored in highlight mode (metadata needs the tiled fetch).")
        highlight_label = highlight_hashtag.upper() if highlight_hashtag else "CONTRIBUTIONS"

        raw_lines = fetch_power_features_meta(
            country=args.country,
            boundary=boundary_wgs84,
            include_minor_lines=args.include_minor_lines,
            include_cables=args.include_cables,
            tile_size_km=args.tile_size_km,
            render_crs=args.crs,
            sea_buffer_km=cable_buffer_km,
            use_cache=not args.no_cache,
            tile_delay=args.tile_delay,
        )
        raw_plants = fetch_power_plants_meta(
            country=args.country,
            boundary=boundary_wgs84,
            tile_size_km=args.tile_size_km,
            render_crs=args.crs,
            use_cache=not args.no_cache,
            tile_delay=args.tile_delay,
        )
        raw_subs = fetch_substations(
            country=args.country,
            boundary=boundary_wgs84,
            tile_size_km=args.tile_size_km,
            render_crs=args.crs,
            use_cache=not args.no_cache,
            tile_delay=args.tile_delay,
        )

        since = args.highlight_since

        highlight_changesets: set[int] = set()
        if highlight_hashtag:
            changeset_ids: set = set()
            for frame in (raw_lines, raw_plants, raw_subs):
                if frame is not None and not frame.empty and "changeset" in frame.columns:
                    recent = frame
                    if since and "timestamp" in frame.columns:
                        # Skip pre-cutoff edits so their changesets are never looked up.
                        recent = frame[frame["timestamp"].fillna("").astype(str) >= since]
                    changeset_ids.update(recent["changeset"].dropna().tolist())
            since_note = f" edited since {since}" if since else ""
            print(f"Resolving #{highlight_hashtag} across {len(changeset_ids):,} changeset(s){since_note}")
            highlight_changesets = resolve_highlight_changesets(
                changeset_ids,
                highlight_hashtag,
                use_cache=not args.no_cache,
                request_delay=args.highlight_request_delay,
            )

        for frame in (raw_lines, raw_plants, raw_subs):
            if frame is not None and not frame.empty:
                frame["is_highlighted"] = assign_is_highlighted(
                    frame, highlight_users, highlight_changesets, since=since
                )

        lines_projected = prepare_lines(
            raw_lines, boundary_wgs84, args.crs, cable_sea_buffer_km=cable_buffer_km
        )
        plants_projected = prepare_plants(
            raw_plants, boundary_wgs84, args.crs, min_capacity_mw=args.min_plant_capacity
        )
        substations_projected = prepare_substations(raw_subs, boundary_wgs84, args.crs)

        def _hl_count(frame) -> int:
            if frame is None or frame.empty or "is_highlighted" not in frame.columns:
                return 0
            return int(frame["is_highlighted"].sum())

        print(
            f"Highlighted ({highlight_label}): {_hl_count(lines_projected):,} line segments, "
            f"{_hl_count(plants_projected):,} plants, {_hl_count(substations_projected):,} substations"
        )
    else:
        if args.single_query:
            raw_lines = fetch_power_features_single(
                country=args.country,
                boundary=boundary_wgs84,
                include_minor_lines=args.include_minor_lines,
                include_cables=args.include_cables,
                sea_buffer_km=cable_buffer_km,
                render_crs=args.crs,
                use_cache=not args.no_cache,
            )
        else:
            raw_lines = fetch_power_features(
                country=args.country,
                boundary=boundary_wgs84,
                include_minor_lines=args.include_minor_lines,
                include_cables=args.include_cables,
                tile_size_km=args.tile_size_km,
                render_crs=args.crs,
                sea_buffer_km=cable_buffer_km,
                use_cache=not args.no_cache,
                tile_delay=args.tile_delay,
            )

        lines_projected = prepare_lines(
            raw_lines, boundary_wgs84, args.crs, cable_sea_buffer_km=cable_buffer_km
        )

        plants_projected = None
        if args.show_plants:
            raw_plants = fetch_power_plants(
                country=args.country,
                boundary=boundary_wgs84,
                tile_size_km=args.tile_size_km,
                render_crs=args.crs,
                use_cache=not args.no_cache,
                tile_delay=args.tile_delay,
            )
            plants_projected = prepare_plants(
                raw_plants, boundary_wgs84, args.crs, min_capacity_mw=args.min_plant_capacity
            )
            print(f"Plants after preparation: {len(plants_projected):,}")

    if args.output:
        fmt = (args.output.suffix.lstrip(".") or args.format[0]).lower()
        if fmt not in {"png", "svg", "pdf"}:
            print(f"Error: cannot infer output format from {args.output} (suffix '{args.output.suffix}')", file=sys.stderr)
            return 2
        outputs = [(args.output, fmt)]
    else:
        outputs = [(output_path(args.country, args.theme, f), f) for f in args.format]

    if args.export_geojson is not None:
        if args.export_geojson:
            geojson_path = Path(args.export_geojson)
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            geojson_path = POSTERS_DIR / f"{slugify(args.country)}_grid_{timestamp}.geojson"
        geojson_path.parent.mkdir(parents=True, exist_ok=True)
        export = lines_projected.to_crs("EPSG:4326").drop(columns=["sort_voltage"], errors="ignore")
        export.to_file(geojson_path, driver="GeoJSON")
        print(f"Saved GeoJSON: {geojson_path}")

    print(f"Rendering {len(lines_projected):,} line segments with theme '{theme.name}'")
    render_poster(
        country=args.country,
        display_country=display_country,
        boundary=boundary_projected,
        lines=lines_projected,
        theme=theme,
        width=width,
        height=height,
        outputs=outputs,
        dpi=args.dpi,
        include_metadata=not args.hide_metadata,
        transparent_background=args.transparent_background,
        title_size=args.title_size,
        include_minor_lines=args.include_minor_lines,
        subtitle=args.subtitle,
        padding=args.padding,
        shift_x=args.shift_x,
        shift_y=args.shift_y,
        hide_borders=args.hide_borders,
        voltage_tiers=args.voltage_tiers,
        logo_image=logo_image,
        logo_size_mm=args.logo_size,
        logo_margin_mm=args.logo_margin,
        logo_alpha=args.logo_alpha,
        fade_top_height=args.fade_top_height,
        fade_top_alpha=args.fade_top_alpha,
        fade_bottom_height=args.fade_bottom_height,
        fade_bottom_alpha=args.fade_bottom_alpha,
        plants=plants_projected,
        plant_marker_scale=args.plant_marker_scale,
        substations=substations_projected,
        highlight_mode=highlight_mode,
        highlight_label=highlight_label,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
