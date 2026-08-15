"""Command-line entry point.

``grid2poster`` geocodes a country or region boundary, downloads the
OpenStreetMap power-line geometries inside it, styles them by voltage, and
renders a print-ready poster.

The pipeline stages live in sibling modules:

- ``common``   - shared constants, the on-disk cache, small utilities
- ``osm_data`` - boundary resolution and Overpass downloads
- ``prepare``  - OSM tag parsing and geometry preparation
- ``theming``  - themes and per-feature styling
- ``render``   - poster composition and file output
"""

import sys
from datetime import datetime
from enum import Enum
from itertools import pairwise
from pathlib import Path
from typing import Annotated

import osmnx as ox
import typer

from .common import (
    DEFAULT_VOLTAGE_TIERS,
    MM_PER_INCH,
    PAPER_SIZES,
    POSTERS_DIR,
    data_dir,
    slugify,
)
from .osm_data import (
    fetch_power_features,
    fetch_power_features_single,
    fetch_power_plants,
    get_country_boundary,
    load_boundary_from_geojson,
)
from .prepare import prepare_lines, prepare_plants
from .render import load_logo_image, render_poster
from .theming import list_themes, load_theme

app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    pretty_exceptions_show_locals=False,
)


class OutputFormat(str, Enum):
    png = "png"
    svg = "svg"
    pdf = "pdf"


PaperSize = Enum("PaperSize", {name: name for name in sorted(PAPER_SIZES)}, type=str)

# Help-panel names, ordered as they should appear in --help.
REGION = "Region"
DATA = "Grid data"
STYLE = "Style"
LAYOUT = "Layout"
OUTPUT = "Output"
NETWORK = "Network"


def output_path(country: str, theme_id: str, fmt: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return POSTERS_DIR / f"{slugify(country)}_grid_{theme_id}_{timestamp}.{fmt}"


def parse_voltage_tiers(value: str) -> tuple[float, float, float, float]:
    """Parse a 'low,mid,high,extra' kV string into a strictly-increasing tuple."""
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if len(parts) != 4:
        raise typer.BadParameter(
            "expected four comma-separated kV values, e.g. 60,150,300,500",
            param_hint="--voltage-tiers",
        )
    try:
        tiers = tuple(float(p) for p in parts)
    except ValueError:
        raise typer.BadParameter(
            f"voltage tiers must be numbers: {value!r}", param_hint="--voltage-tiers"
        ) from None
    if tiers[0] <= 0:
        raise typer.BadParameter("voltage tiers must be positive", param_hint="--voltage-tiers")
    if any(a >= b for a, b in pairwise(tiers)):
        raise typer.BadParameter(
            f"voltage tiers must strictly increase: {value!r}", param_hint="--voltage-tiers"
        )
    return tiers  # type: ignore[return-value]


def parse_formats(values: list[str]) -> list[str]:
    """Accept repeated flags (-f png -f svg) and comma-separated lists (-f png,svg)."""
    formats: list[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip().lower()
            if not part:
                continue
            if part not in {f.value for f in OutputFormat}:
                raise typer.BadParameter(
                    f"unknown format {part!r}, expected one of: png, svg, pdf",
                    param_hint="--format",
                )
            if part not in formats:
                formats.append(part)
    return formats or [OutputFormat.png.value, OutputFormat.svg.value]


def _is_format_token(token: str) -> bool:
    """True for a bare ``png`` / ``svg,pdf`` style value (never an option)."""
    if token.startswith("-"):
        return False
    parts = [p.strip().lower() for p in token.split(",")]
    return all(p in {f.value for f in OutputFormat} for p in parts if p)


def normalize_legacy_argv(argv: list[str]) -> list[str]:
    """Translate the pre-Typer argparse syntax into its Typer equivalent.

    Click has no variadic options and no optional-value options, so two flags
    would otherwise have changed shape. Rewriting the argument list keeps every
    documented invocation of the old ``create_grid_poster.py`` CLI working:

    - ``--format png svg`` (argparse ``nargs="+"``) becomes ``--format png,svg``
    - ``--export-geojson PATH`` (argparse ``nargs="?"``) becomes
      ``--export-geojson-path PATH``; the bare flag is left alone.
    """
    out: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]

        if (
            token in {"--format", "-f"}
            and index + 1 < len(argv)
            and _is_format_token(argv[index + 1])
        ):
            values: list[str] = []
            index += 1
            while index < len(argv) and _is_format_token(argv[index]):
                values.append(argv[index])
                index += 1
            out.extend([token, ",".join(values)])
            continue

        if (
            token == "--export-geojson"
            and index + 1 < len(argv)
            and not argv[index + 1].startswith("-")
        ):
            out.extend(["--export-geojson-path", argv[index + 1]])
            index += 2
            continue

        if token.startswith("--export-geojson="):
            value = token.split("=", 1)[1]
            out.append("--export-geojson" if not value else "--export-geojson-path")
            if value:
                out.append(value)
            index += 1
            continue

        out.append(token)
        index += 1
    return out


def run() -> None:
    """Console-script entry point: normalize legacy syntax, then hand off to Typer."""
    app(args=normalize_legacy_argv(sys.argv[1:]))


def list_regions() -> None:
    directory = data_dir("regions")
    print(f"Available regions ({directory}):\n")
    for path in sorted(directory.glob("*.geojson")):
        print(f"- {path.stem}")
    print("\nPass one to --boundary-geojson, by name or by path.")


def resolve_boundary_geojson(value: Path) -> Path:
    """Accept a filesystem path or the bare name of a bundled region."""
    if value.exists():
        return value
    candidate = data_dir("regions") / f"{value.name.removesuffix('.geojson')}.geojson"
    if candidate.exists():
        return candidate
    raise typer.BadParameter(
        f"no such file {value}, and no bundled region named '{value.stem}' (see --list-regions)",
        param_hint="--boundary-geojson",
    )


@app.command()
def main(
    country: Annotated[
        str | None,
        typer.Option(
            "--country",
            "-C",
            help="Country or region name resolvable by Nominatim.",
            rich_help_panel=REGION,
        ),
    ] = None,
    boundary_geojson: Annotated[
        Path | None,
        typer.Option(
            help="Load the boundary polygon(s) from a local GeoJSON file instead of geocoding "
            "via Nominatim. Accepts a path or the name of a bundled region (--list-regions). "
            "All polygonal features in the file are dissolved into a single boundary.",
            rich_help_panel=REGION,
        ),
    ] = None,
    internal_borders: Annotated[
        bool,
        typer.Option(
            "--internal-borders",
            help="When loading --boundary-geojson, keep each feature as a separate part so "
            "borders shared between adjacent features (e.g. provinces) are drawn instead of "
            "dissolved away.",
            rich_help_panel=REGION,
        ),
    ] = False,
    include_outlying: Annotated[
        bool,
        typer.Option(
            "--include-outlying",
            help="Keep overseas territories and other polygons far from the main landmass. "
            "By default only the mainland (and nearby islands) is rendered.",
            rich_help_panel=REGION,
        ),
    ] = False,
    list_themes_flag: Annotated[
        bool,
        typer.Option(
            "--list-themes", help="List available themes and exit.", rich_help_panel=REGION
        ),
    ] = False,
    list_regions_flag: Annotated[
        bool,
        typer.Option(
            "--list-regions",
            help="List bundled region boundaries and exit.",
            rich_help_panel=REGION,
        ),
    ] = False,
    include_minor_lines: Annotated[
        bool,
        typer.Option(
            "--include-minor-lines",
            help="Also fetch power=minor_line.",
            rich_help_panel=DATA,
        ),
    ] = False,
    include_cables: Annotated[
        bool,
        typer.Option(
            "--include-cables/--no-include-cables",
            help="Fetch power=cable features (underground/submarine).",
            rich_help_panel=DATA,
        ),
    ] = False,
    cable_sea_buffer_km: Annotated[
        float,
        typer.Option(
            help="When --include-cables is on, inflate the boundary by this many kilometers over "
            "water so submarine cables between islands and to neighboring countries are queried "
            "from Overpass and survive coastline clipping. Set to 0 to disable.",
            rich_help_panel=DATA,
        ),
    ] = 200.0,
    voltage_tiers: Annotated[
        str,
        typer.Option(
            metavar="LOW,MID,HIGH,EXTRA",
            help="Lower kV bounds for the four voltage tiers, comma-separated. Sets how lines are "
            "colored/weighted and the legend labels; tune to the grid being mapped.",
            rich_help_panel=DATA,
        ),
    ] = ",".join(str(int(t)) for t in DEFAULT_VOLTAGE_TIERS),
    show_plants: Annotated[
        bool,
        typer.Option(
            "--show-plants",
            help="Fetch power=plant features and overlay them as markers sized by capacity "
            "(plant:output:electricity) and colored by source (plant:source).",
            rich_help_panel=DATA,
        ),
    ] = False,
    min_plant_capacity: Annotated[
        float,
        typer.Option(
            metavar="MW",
            help="Only draw plants with at least this electrical output in MW. Plants with "
            "unknown capacity are dropped when set. Default 0 (show all).",
            rich_help_panel=DATA,
        ),
    ] = 0.0,
    theme: Annotated[
        str,
        typer.Option("--theme", "-t", help="Theme ID from themes/.", rich_help_panel=STYLE),
    ] = "paper_grid",
    display_country: Annotated[
        str | None,
        typer.Option(help="Text to print on the poster.", rich_help_panel=STYLE),
    ] = None,
    subtitle: Annotated[
        str | None,
        typer.Option(
            help="Override the poster subtitle (default: 'ELECTRICAL TRANSMISSION GRID', or "
            "'ELECTRICAL GRID' with --include-minor-lines).",
            rich_help_panel=STYLE,
        ),
    ] = None,
    title_size: Annotated[
        float | None,
        typer.Option(
            help="Title font size in points. Defaults to an auto-scaled value based on poster size.",
            rich_help_panel=STYLE,
        ),
    ] = None,
    hide_metadata: Annotated[
        bool,
        typer.Option(
            "--hide-metadata", help="Do not print segment counts on poster.", rich_help_panel=STYLE
        ),
    ] = False,
    hide_borders: Annotated[
        bool,
        typer.Option(
            "--hide-borders",
            help="Do not draw the region boundary outline.",
            rich_help_panel=STYLE,
        ),
    ] = False,
    transparent_background: Annotated[
        bool,
        typer.Option(
            "--transparent-background",
            help="Render with a transparent background instead of the theme background color. The "
            "grid, text, and fades are kept; only the backdrop is made transparent. Best used with "
            "PNG or SVG output (PDF transparency support is limited).",
            rich_help_panel=STYLE,
        ),
    ] = False,
    plant_marker_scale: Annotated[
        float,
        typer.Option(
            help="Multiplier for plant marker sizes. Increase for sparse grids, decrease to "
            "reduce clutter.",
            rich_help_panel=STYLE,
        ),
    ] = 1.0,
    fade_top_height: Annotated[
        float,
        typer.Option(
            help="Fraction of the poster height covered by the top fade-to-background gradient. "
            "Lower = shorter fade; 0 disables it.",
            rich_help_panel=STYLE,
        ),
    ] = 0.28,
    fade_top_alpha: Annotated[
        float,
        typer.Option(
            help="Opacity of the top fade at the poster edge, 0 (none) to 1 (fully opaque). "
            "Lower = lighter fade.",
            rich_help_panel=STYLE,
        ),
    ] = 1.0,
    fade_bottom_height: Annotated[
        float,
        typer.Option(
            help="Fraction of the poster height covered by the bottom fade gradient. "
            "Lower = shorter fade; 0 disables it.",
            rich_help_panel=STYLE,
        ),
    ] = 0.28,
    fade_bottom_alpha: Annotated[
        float,
        typer.Option(
            help="Opacity of the bottom fade at the poster edge, 0 (none) to 1 (fully opaque). "
            "Lower = lighter fade.",
            rich_help_panel=STYLE,
        ),
    ] = 1.0,
    logo: Annotated[
        Path | None,
        typer.Option(
            help="Path to an SVG or PNG logo to place in the lower-left corner.",
            rich_help_panel=STYLE,
        ),
    ] = None,
    logo_size: Annotated[
        float,
        typer.Option(
            help="Logo width in millimeters (height scales to preserve the aspect ratio).",
            rich_help_panel=STYLE,
        ),
    ] = 20.0,
    logo_margin: Annotated[
        float,
        typer.Option(
            help="Margin in millimeters between the logo and the lower-left poster edges.",
            rich_help_panel=STYLE,
        ),
    ] = 12.0,
    logo_alpha: Annotated[
        float,
        typer.Option(
            help="Logo opacity from 0 (transparent) to 1 (fully opaque).", rich_help_panel=STYLE
        ),
    ] = 1.0,
    paper_size: Annotated[
        PaperSize | None,
        typer.Option(
            help="Preset paper size in portrait orientation. Overrides --width and --height. "
            "Use --landscape to flip orientation.",
            rich_help_panel=LAYOUT,
        ),
    ] = None,
    width: Annotated[
        float,
        typer.Option(
            "--width",
            "-W",
            help="Poster width in millimeters (A3 short side).",
            rich_help_panel=LAYOUT,
        ),
    ] = 297.0,
    height: Annotated[
        float,
        typer.Option(
            "--height",
            "-H",
            help="Poster height in millimeters (A3 long side).",
            rich_help_panel=LAYOUT,
        ),
    ] = 420.0,
    landscape: Annotated[
        bool,
        typer.Option(
            "--landscape",
            help="Render in landscape (horizontal) orientation. Swaps width and height if "
            "width < height.",
            rich_help_panel=LAYOUT,
        ),
    ] = False,
    padding: Annotated[
        float,
        typer.Option(
            help="Fractional padding around the boundary bounds. Lower = more zoomed in "
            "(e.g. 0 = tight fit, -0.05 = crop slightly into the bounds, 0.20 = looser).",
            rich_help_panel=LAYOUT,
        ),
    ] = 0.10,
    shift_x: Annotated[
        float,
        typer.Option(
            help="Shift the grid data horizontally on the poster, as a fraction of the data "
            "extent. Positive values shift right, negative shift left (e.g. 0.1 = shift 10% right).",
            rich_help_panel=LAYOUT,
        ),
    ] = 0.0,
    shift_y: Annotated[
        float,
        typer.Option(
            help="Shift the grid data vertically on the poster, as a fraction of the data extent. "
            "Positive values shift up, negative shift down (e.g. 0.1 = shift 10% up).",
            rich_help_panel=LAYOUT,
        ),
    ] = 0.0,
    crs: Annotated[
        str,
        typer.Option(
            help="Projection used for rendering. EPSG:3857 Pseudo-Mercator works well for "
            "country posters.",
            rich_help_panel=LAYOUT,
        ),
    ] = "EPSG:3857",
    dpi: Annotated[int, typer.Option(help="Raster output DPI.", rich_help_panel=LAYOUT)] = 300,
    output_format: Annotated[
        list[str] | None,
        typer.Option(
            "--format",
            "-f",
            metavar="FORMAT",
            help="Output format(s): png, svg or pdf. Repeat the flag or comma-separate to write "
            "several formats at once.  [default: png,svg]",
            rich_help_panel=OUTPUT,
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Output file path. When set, only a single file is written and its format is "
            "inferred from the extension.",
            rich_help_panel=OUTPUT,
        ),
    ] = None,
    export_geojson: Annotated[
        bool,
        typer.Option(
            "--export-geojson",
            help="Also save all transmission lines as a single GeoJSON (WGS84), written next to "
            "the poster unless --export-geojson-path is given.",
            rich_help_panel=OUTPUT,
        ),
    ] = False,
    export_geojson_path: Annotated[
        Path | None,
        typer.Option(
            help="Write the GeoJSON export to this path (implies --export-geojson).",
            rich_help_panel=OUTPUT,
        ),
    ] = None,
    tile_size_km: Annotated[
        float,
        typer.Option(
            help="Overpass query tile size in kilometers. Use smaller values for very large "
            "countries or busy servers.",
            rich_help_panel=NETWORK,
        ),
    ] = 400.0,
    single_query: Annotated[
        bool,
        typer.Option(
            "--single-query",
            help="Fetch all power features in a single Overpass query instead of tiling. Faster "
            "for small/medium regions but may time out on large countries or continents.",
            rich_help_panel=NETWORK,
        ),
    ] = False,
    tile_delay: Annotated[
        float,
        typer.Option(
            help="Seconds to wait between Overpass tile API requests. Useful to avoid "
            "rate-limiting on busy public endpoints.",
            rich_help_panel=NETWORK,
        ),
    ] = 30.0,
    overpass_endpoint: Annotated[
        str | None,
        typer.Option(
            help="Override the Overpass API endpoint. Use a mirror when the default "
            "(overpass-api.de) is rate-limiting or refusing connections. Examples: "
            "https://overpass.kumi.systems/api/interpreter, "
            "https://overpass.private.coffee/api/interpreter, "
            "https://overpass.osm.ch/api/interpreter.",
            rich_help_panel=NETWORK,
        ),
    ] = None,
    no_cache: Annotated[
        bool,
        typer.Option(
            "--no-cache",
            help="Ignore cached boundaries and OSM power features on this run. Fresh results are "
            "still written to the cache for future runs.",
            rich_help_panel=NETWORK,
        ),
    ] = False,
    verbose_osmnx: Annotated[
        bool,
        typer.Option("--verbose-osmnx", help="Print OSMnx request logs.", rich_help_panel=NETWORK),
    ] = False,
) -> None:
    """Create country electrical transmission grid posters from OpenStreetMap power=line data."""
    if list_themes_flag:
        list_themes()
        return
    if list_regions_flag:
        list_regions()
        return
    if not country:
        raise typer.BadParameter(
            "--country is required unless --list-themes or --list-regions is used",
            param_hint="--country",
        )

    tiers = parse_voltage_tiers(voltage_tiers)
    formats = parse_formats(output_format or [])

    ox.settings.use_cache = not no_cache
    ox.settings.log_console = bool(verbose_osmnx)
    ox.settings.requests_timeout = 180
    if overpass_endpoint:
        ox.settings.overpass_url = overpass_endpoint
        print(f"Using Overpass endpoint: {overpass_endpoint}")
    # Keep OSMnx's own guard reasonably high: we explicitly tile the country
    # boundary below, so this setting is only a secondary safety net.
    ox.settings.max_query_area_size = max(
        ox.settings.max_query_area_size, (tile_size_km * 1000) ** 2 * 2
    )

    poster_theme = load_theme(theme)
    display_name = display_country or country

    logo_image = None
    if logo is not None:
        if not logo.exists():
            raise typer.BadParameter(f"logo file not found: {logo}", param_hint="--logo")
        logo_image = load_logo_image(logo)

    if paper_size is not None:
        width_mm, height_mm = PAPER_SIZES[paper_size.value]
    else:
        width_mm, height_mm = width, height
    if landscape and width_mm < height_mm:
        width_mm, height_mm = height_mm, width_mm
    width_in, height_in = width_mm / MM_PER_INCH, height_mm / MM_PER_INCH

    if boundary_geojson:
        boundary_path = resolve_boundary_geojson(boundary_geojson)
        print(f"Loading boundary from {boundary_path}")
        boundary_wgs84 = load_boundary_from_geojson(
            boundary_path, country, keep_internal_borders=internal_borders
        )
    else:
        boundary_wgs84 = get_country_boundary(
            country,
            mainland_only=not include_outlying,
            use_cache=not no_cache,
        )
    cable_buffer_km = cable_sea_buffer_km if include_cables else 0.0
    if single_query:
        raw_lines = fetch_power_features_single(
            country=country,
            boundary=boundary_wgs84,
            include_minor_lines=include_minor_lines,
            include_cables=include_cables,
            sea_buffer_km=cable_buffer_km,
            render_crs=crs,
            use_cache=not no_cache,
        )
    else:
        raw_lines = fetch_power_features(
            country=country,
            boundary=boundary_wgs84,
            include_minor_lines=include_minor_lines,
            include_cables=include_cables,
            tile_size_km=tile_size_km,
            render_crs=crs,
            sea_buffer_km=cable_buffer_km,
            use_cache=not no_cache,
            tile_delay=tile_delay,
        )

    boundary_projected = boundary_wgs84.to_crs(crs)
    lines_projected = prepare_lines(
        raw_lines, boundary_wgs84, crs, cable_sea_buffer_km=cable_buffer_km
    )

    plants_projected = None
    if show_plants:
        raw_plants = fetch_power_plants(
            country=country,
            boundary=boundary_wgs84,
            tile_size_km=tile_size_km,
            render_crs=crs,
            use_cache=not no_cache,
            tile_delay=tile_delay,
        )
        plants_projected = prepare_plants(
            raw_plants, boundary_wgs84, crs, min_capacity_mw=min_plant_capacity
        )
        print(f"Plants after preparation: {len(plants_projected):,}")

    if output:
        fmt = (output.suffix.lstrip(".") or formats[0]).lower()
        if fmt not in {f.value for f in OutputFormat}:
            raise typer.BadParameter(
                f"cannot infer output format from {output} (suffix '{output.suffix}')",
                param_hint="--output",
            )
        outputs = [(output, fmt)]
    else:
        outputs = [(output_path(country, theme, f), f) for f in formats]

    if export_geojson or export_geojson_path is not None:
        if export_geojson_path is not None:
            geojson_path = export_geojson_path
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            geojson_path = POSTERS_DIR / f"{slugify(country)}_grid_{timestamp}.geojson"
        geojson_path.parent.mkdir(parents=True, exist_ok=True)
        export = lines_projected.to_crs("EPSG:4326").drop(columns=["sort_voltage"], errors="ignore")
        export.to_file(geojson_path, driver="GeoJSON")
        print(f"Saved GeoJSON: {geojson_path}")

    print(f"Rendering {len(lines_projected):,} line segments with theme '{poster_theme.name}'")
    render_poster(
        country=country,
        display_country=display_name,
        boundary=boundary_projected,
        lines=lines_projected,
        theme=poster_theme,
        width=width_in,
        height=height_in,
        outputs=outputs,
        dpi=dpi,
        include_metadata=not hide_metadata,
        transparent_background=transparent_background,
        title_size=title_size,
        include_minor_lines=include_minor_lines,
        subtitle=subtitle,
        padding=padding,
        shift_x=shift_x,
        shift_y=shift_y,
        hide_borders=hide_borders,
        voltage_tiers=tiers,
        logo_image=logo_image,
        logo_size_mm=logo_size,
        logo_margin_mm=logo_margin,
        logo_alpha=logo_alpha,
        fade_top_height=fade_top_height,
        fade_top_alpha=fade_top_alpha,
        fade_bottom_height=fade_bottom_height,
        fade_bottom_alpha=fade_bottom_alpha,
        plants=plants_projected,
        plant_marker_scale=plant_marker_scale,
    )


if __name__ == "__main__":
    run()
