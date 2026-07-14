#!/usr/bin/env python3
"""
GridToPoster time-lapse
========================
Render a multi-year animated GIF showing how OpenStreetMap `power=line`
mapping for a country/region has grown over time.

Reuses the same boundary-resolution, Overpass-fetch, and poster-rendering
pipeline as ``create_grid_poster.py`` (see ``add_common_poster_arguments`` and
``fetch_lines_and_plants`` there), looping it once per snapshot year with an
Overpass ``date:`` (attic data) filter, then assembling the resulting PNG
frames into a GIF with Pillow.

Caveats:

- The country/region *boundary* is resolved once (current-day borders) and
  reused for every frame — Nominatim/Natural Earth have no historical
  boundary data, and borders are stable enough at this granularity for a
  multi-year time-lapse.
- Historical ("attic") Overpass queries are noticeably more timeout-prone on
  the public overpass-api.de instance than current-data queries. The tiled
  fetch path (the default; avoid --single-query here) has retry/backoff built
  in and will absorb this, but continent-scale, many-year runs are slow.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

import osmnx as ox
from PIL import Image

from common import MM_PER_INCH, PAPER_SIZES, POSTERS_DIR, slugify
from create_grid_poster import add_common_poster_arguments, fetch_lines_and_plants
from osm_data import get_country_boundary, load_boundary_from_geojson
from render import load_logo_image, render_poster
from theming import load_theme

MONTH_DAY_RE = re.compile(r"^\d{2}-\d{2}$")


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a time-lapse GIF of OpenStreetMap electrical grid data across multiple years.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--country", "-C", required=True, help="Country or region name resolvable by Nominatim")
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
    parser.add_argument("--display-country", help="Text to print on each frame")
    parser.add_argument(
        "--subtitle",
        help="Override the poster subtitle (default: 'ELECTRICAL TRANSMISSION GRID', "
             "or 'ELECTRICAL GRID' with --include-minor-lines)",
    )
    parser.add_argument(
        "--start-year",
        type=int,
        default=None,
        help="First snapshot year (default: --end-year minus 10)",
    )
    parser.add_argument(
        "--end-year",
        type=int,
        default=None,
        help="Last snapshot year, inclusive (default: current year)",
    )
    parser.add_argument(
        "--step-years",
        type=int,
        default=1,
        help="Year interval between frames. --end-year is always included as the final frame.",
    )
    parser.add_argument(
        "--month-day",
        default="01-01",
        metavar="MM-DD",
        help="Snapshot date used within each year, e.g. '06-30' for mid-year snapshots.",
    )
    parser.add_argument(
        "--frame-duration-ms",
        type=int,
        default=800,
        help="Per-frame display time in the GIF, in milliseconds.",
    )
    parser.add_argument(
        "--end-hold-ms",
        type=int,
        default=1600,
        help="Extra hold time added to the final frame, in milliseconds.",
    )
    parser.add_argument("--loop", type=int, default=0, help="GIF loop count (0 = loop forever)")
    parser.add_argument("--output", "-o", type=Path, default=None, help="Output GIF path")
    parser.add_argument(
        "--frames-dir",
        type=Path,
        default=None,
        help="Directory to write per-year PNG frames (default: a folder next to the GIF, "
             "removed afterward unless --keep-frames is passed).",
    )
    parser.add_argument(
        "--keep-frames",
        action="store_true",
        help="Keep the individual PNG frames instead of deleting them after the GIF is assembled.",
    )
    add_common_poster_arguments(parser)
    # GIFs get large fast at poster-quality DPI/paper size; default to something
    # web-shareable while leaving every flag fully overridable.
    parser.set_defaults(dpi=120, width=210.0, height=297.0)
    return parser.parse_args(list(argv))


def year_range(start_year: int, end_year: int, step_years: int) -> list[int]:
    if step_years <= 0:
        raise ValueError("--step-years must be positive")
    if start_year > end_year:
        raise ValueError("--start-year must be <= --end-year")
    years = list(range(start_year, end_year + 1, step_years))
    if years[-1] != end_year:
        years.append(end_year)
    return years


def main(argv: Iterable[str] = sys.argv[1:]) -> int:
    args = parse_args(argv)

    if not MONTH_DAY_RE.match(args.month_day):
        print(f"Error: --month-day must look like 'MM-DD', got {args.month_day!r}", file=sys.stderr)
        return 2

    end_year = args.end_year if args.end_year is not None else datetime.now().year
    start_year = args.start_year if args.start_year is not None else end_year - 10
    try:
        years = year_range(start_year, end_year, args.step_years)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if len(years) < 2:
        print("Warning: only one snapshot year requested - the output GIF will have a single frame")

    ox.settings.use_cache = not args.no_cache
    ox.settings.log_console = bool(args.verbose_osmnx)
    ox.settings.requests_timeout = args.overpass_timeout
    if args.overpass_endpoint:
        ox.settings.overpass_url = args.overpass_endpoint
        print(f"Using Overpass endpoint: {args.overpass_endpoint}")
    ox.settings.max_query_area_size = max(ox.settings.max_query_area_size, (args.tile_size_km * 1000) ** 2 * 2)

    if args.transparent_background:
        print(
            "Note: --transparent-background is ignored for GIF frames (classic GIF "
            "transparency doesn't animate well against anti-aliased lines); rendering opaque frames."
        )

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
            args.country, mainland_only=not args.include_outlying, use_cache=not args.no_cache,
        )
    boundary_projected = boundary_wgs84.to_crs(args.crs)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    gif_path = args.output or POSTERS_DIR / f"{slugify(args.country)}_grid_{args.theme}_timelapse_{timestamp}.gif"
    gif_path.parent.mkdir(parents=True, exist_ok=True)

    keep_frames = args.keep_frames or args.frames_dir is not None
    frames_dir = args.frames_dir or gif_path.parent / f"{gif_path.stem}_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    print(f"Building a {len(years)}-frame time-lapse for {args.country}: {years}")
    frame_paths: list[Path] = []
    for year in years:
        historical_date = f"{year:04d}-{args.month_day}T00:00:00Z"
        print(f"\n=== {year} (Overpass date={historical_date}) ===")
        lines_projected, plants_projected = fetch_lines_and_plants(
            args, boundary_wgs84, historical_date=historical_date
        )
        frame_path = frames_dir / f"frame_{year:04d}.png"
        print(f"Rendering {len(lines_projected):,} line segments for {year}")
        render_poster(
            country=args.country,
            display_country=display_country,
            boundary=boundary_projected,
            lines=lines_projected,
            theme=theme,
            width=width,
            height=height,
            outputs=[(frame_path, "png")],
            dpi=args.dpi,
            include_metadata=not args.hide_metadata,
            transparent_background=False,
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
            as_of_year=year,
            emphasize_year=True,
        )
        frame_paths.append(frame_path)

    print(f"\nAssembling GIF from {len(frame_paths)} frame(s) -> {gif_path}")
    frames = [Image.open(path).convert("RGB") for path in frame_paths]
    durations = [args.frame_duration_ms] * len(frames)
    durations[-1] += args.end_hold_ms
    frames[0].save(
        gif_path,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=args.loop,
        optimize=True,
    )
    print(f"Saved time-lapse GIF: {gif_path}")

    if keep_frames:
        print(f"Kept frame PNGs in: {frames_dir}")
    else:
        shutil.rmtree(frames_dir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
