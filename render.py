"""Poster composition: extent, gradient fades, text overlays, logo, and output."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.font_manager import FontProperties
from matplotlib.offsetbox import AnchoredOffsetbox, HPacker, TextArea

from common import DEFAULT_VOLTAGE_TIERS, MM_PER_INCH, tqdm
from prepare import PLANT_SOURCE_BUCKETS
from theming import (
    Theme,
    compute_line_styles,
    compute_plant_styles,
    compute_substation_styles,
    derive_plant_colors,
    substation_colors,
)

# Half (thin) space placed between a number and its unit, e.g. "380 kV".
# Rendered via matplotlib mathtext, where "\," is a thin space.
THIN_SPACE = r"$\,$"

# Z-order for the title, subtitle, metadata and credit overlays. Sits well above
# the data lines (zorder ~2–8) and the gradient fades (10) so the text always
# renders on top of the grid.
TEXT_ZORDER = 100


def set_country_extent(
    ax: plt.Axes,
    boundary: gpd.GeoDataFrame,
    width: float,
    height: float,
    padding: float,
    shift_x: float = 0.0,
    shift_y: float = 0.0,
) -> None:
    minx, miny, maxx, maxy = boundary.total_bounds
    xmid = (minx + maxx) / 2
    ymid = (miny + maxy) / 2
    xspan = max(maxx - minx, 1.0)
    yspan = max(maxy - miny, 1.0)

    xspan *= 1 + padding
    yspan *= 1 + padding

    poster_aspect = width / height
    data_aspect = xspan / yspan

    if data_aspect > poster_aspect:
        yspan = xspan / poster_aspect
    else:
        xspan = yspan * poster_aspect

    xmid -= shift_x * xspan
    ymid -= shift_y * yspan

    ax.set_xlim(xmid - xspan / 2, xmid + xspan / 2)
    ax.set_ylim(ymid - yspan / 2, ymid + yspan / 2)


def add_gradient_fade(
    ax: plt.Axes,
    color: str,
    where: str,
    zorder: int = 10,
    height: float = 0.28,
    max_alpha: float = 1.0,
) -> None:
    # ``height`` is the fraction of the poster height the fade band covers
    # (0 disables it); ``max_alpha`` is the opacity at the poster edge (1 =
    # fully opaque). Both are clamped so out-of-range CLI values stay sane.
    height = float(min(max(height, 0.0), 1.0))
    max_alpha = float(min(max(max_alpha, 0.0), 1.0))
    if height <= 0.0 or max_alpha <= 0.0:
        return

    vals = np.linspace(0, 1, 256).reshape(-1, 1)
    gradient = np.hstack((vals, vals))
    rgb = mcolors.to_rgb(color)
    rgba = np.zeros((256, 4))
    rgba[:, :3] = rgb

    if where == "bottom":
        rgba[:, 3] = np.linspace(max_alpha, 0, 256)
        y0, y1 = 0.0, height
    elif where == "top":
        rgba[:, 3] = np.linspace(0, max_alpha, 256)
        y0, y1 = 1.0 - height, 1.0
    else:
        raise ValueError("where must be 'top' or 'bottom'")

    cmap = mcolors.ListedColormap(rgba)
    xlim = ax.get_xlim()
    ylim = ax.get_ylim()
    yspan = ylim[1] - ylim[0]
    ax.imshow(
        gradient,
        extent=[xlim[0], xlim[1], ylim[0] + yspan * y0, ylim[0] + yspan * y1],
        aspect="auto",
        cmap=cmap,
        zorder=zorder,
        origin="lower",
    )


def spaced_upper(text: str) -> str:
    return " ".join(text.upper()) if len(text) <= 20 else text.upper()


def load_logo_image(path: Path, render_px: int = 1024) -> np.ndarray:
    """Load a PNG or SVG logo as an RGBA float array in [0, 1].

    SVGs are rasterized with cairosvg at ``render_px`` width (aspect preserved)
    so they stay crisp when downscaled; PNG/JPEG files are read as-is.
    """
    suffix = path.suffix.lower()
    if suffix == ".svg":
        try:
            import cairosvg
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise SystemExit(
                "Rendering an SVG logo requires the 'cairosvg' package. "
                "Install it with 'pip install cairosvg', or pass a PNG logo instead."
            ) from exc
        import io

        png_bytes = cairosvg.svg2png(url=str(path), output_width=render_px)
        arr = plt.imread(io.BytesIO(png_bytes), format="png")
    elif suffix in {".png", ".jpg", ".jpeg"}:
        arr = plt.imread(path)
    else:
        raise SystemExit(f"Unsupported logo format '{suffix}'. Use an SVG or PNG file.")

    arr = np.asarray(arr, dtype=float)
    if arr.max() > 1.0:  # 8-bit images come back in [0, 255]
        arr = arr / 255.0
    # Normalize everything to RGBA so an alpha multiplier applies uniformly.
    if arr.ndim == 2:  # grayscale
        arr = np.dstack([arr, arr, arr, np.ones_like(arr)])
    elif arr.shape[2] == 3:  # RGB without alpha
        arr = np.dstack([arr, np.ones(arr.shape[:2])])
    return arr


def add_logo(
    fig: plt.Figure,
    image: np.ndarray,
    width: float,
    height: float,
    size_mm: float,
    margin_mm: float,
    alpha: float = 1.0,
) -> None:
    """Place ``image`` in the lower-left corner of the figure.

    ``width``/``height`` are the figure size in inches. ``size_mm`` is the logo
    width in millimeters (its height scales to preserve the aspect ratio) and
    ``margin_mm`` is the gap to the bottom and left edges. A dedicated inset axes
    in figure-fraction coordinates keeps the physical size exact across paper
    sizes and output formats.
    """
    img_h, img_w = image.shape[:2]
    size_in = size_mm / MM_PER_INCH
    margin_in = margin_mm / MM_PER_INCH

    w_frac = size_in / width
    h_frac = (size_in * img_h / img_w) / height
    left_frac = margin_in / width
    bottom_frac = margin_in / height

    inset = fig.add_axes((left_frac, bottom_frac, w_frac, h_frac))
    inset.axis("off")
    inset.set_zorder(TEXT_ZORDER + 1)

    rgba = image
    if alpha < 1.0:
        rgba = image.copy()
        rgba[..., 3] = rgba[..., 3] * alpha
    # The axes box already matches the image aspect ratio, so aspect="auto"
    # fills it without distortion.
    inset.imshow(rgba, aspect="auto", interpolation="antialiased", zorder=TEXT_ZORDER + 1)


def render_poster(
    country: str,
    display_country: str,
    boundary: gpd.GeoDataFrame,
    lines: gpd.GeoDataFrame,
    theme: Theme,
    width: float,
    height: float,
    outputs: list[tuple[Path, str]],
    dpi: int,
    include_metadata: bool,
    transparent_background: bool = False,
    title_size: float | None = None,
    include_minor_lines: bool = False,
    subtitle: str | None = None,
    padding: float = 0.10,
    shift_x: float = 0.0,
    shift_y: float = 0.0,
    hide_borders: bool = False,
    voltage_tiers: tuple[float, float, float, float] = DEFAULT_VOLTAGE_TIERS,
    logo_image: np.ndarray | None = None,
    logo_size_mm: float = 20.0,
    logo_margin_mm: float = 12.0,
    logo_alpha: float = 1.0,
    fade_top_height: float = 0.28,
    fade_top_alpha: float = 1.0,
    fade_bottom_height: float = 0.28,
    fade_bottom_alpha: float = 1.0,
    plants: gpd.GeoDataFrame | None = None,
    plant_marker_scale: float = 1.0,
    substations: gpd.GeoDataFrame | None = None,
    highlight_mode: bool = False,
    highlight_label: str | None = None,
) -> None:
    # A transparent backdrop keeps the grid, text and fades but drops the solid
    # theme background, so the poster can be composited over other artwork.
    bg_color = "none" if transparent_background else theme.bg
    fig, ax = plt.subplots(figsize=(width, height), facecolor=bg_color)
    ax.set_facecolor(bg_color)
    ax.set_position((0, 0, 1, 1))
    ax.axis("off")

    if not hide_borders:
        boundary.plot(ax=ax, facecolor="none", edgecolor=theme.boundary, linewidth=0.7, alpha=0.9, zorder=1)

    styled = lines.assign(**compute_line_styles(
        lines,
        theme,
        voltage_tiers=voltage_tiers,
        highlight_mode=highlight_mode,
    ))

    def _draw_line_groups(frame: gpd.GeoDataFrame, zorder_fn, desc: str) -> None:
        if frame.empty:
            return
        grouped = frame.groupby(["_color", "_linewidth", "_alpha"], sort=False)
        for (color, linewidth, alpha), group in tqdm(
            grouped, total=grouped.ngroups, desc=desc, unit="group", leave=True
        ):
            group.plot(ax=ax, color=color, linewidth=linewidth, alpha=alpha, zorder=zorder_fn(group))

    # Higher-voltage groups draw on top of lower ones so the backbone reads
    # clearly. Cap the contribution well below the gradient-fade band (10) and
    # the title/overlay band (TEXT_ZORDER) so a mis-tagged voltage value (e.g. an
    # OSM tag in volts that slips through as a huge kV) can never push a line
    # group on top of the title text.
    def _voltage_zorder(group: gpd.GeoDataFrame) -> float:
        return 2 + min(group["sort_voltage"].max() / 1000.0, 6.0)

    if highlight_mode and "is_highlighted" in styled.columns:
        hi_mask = styled["is_highlighted"].to_numpy(dtype=bool)
        # Dimmed base first, then highlighted contributions at a fixed higher
        # zorder (8.5) so they always sit on top of all dimmed base lines but
        # still below the fades (10) and text (TEXT_ZORDER).
        _draw_line_groups(styled[~hi_mask], _voltage_zorder, "Rendering base lines")
        _draw_line_groups(styled[hi_mask], lambda _g: 8.5, "Rendering highlighted lines")
    else:
        _draw_line_groups(styled, _voltage_zorder, "Rendering line groups")

    highlight_color = theme.highlight or theme.line_extra

    plant_color_map: dict[str, str] = {}
    if plants is not None and not plants.empty:
        plant_color_map = derive_plant_colors(theme)
        plant_styles = compute_plant_styles(
            plants, theme, marker_scale=plant_marker_scale, color_map=plant_color_map
        )
        styled_plants = plants.assign(**plant_styles)
        edge_color = theme.plant_edge if theme.plant_edge is not None else bg_color

        def _scatter_plants(frame: gpd.GeoDataFrame, zorder: float, alpha: float, color_override=None) -> None:
            # One scatter per source bucket (mirroring the grouped line plotting)
            # unless an override paints the whole subset a single color. zorder 9+
            # sits above every line group (capped at 8) but under the gradient
            # fades (10) so markers dim toward the poster edges.
            if frame.empty:
                return
            if color_override is not None:
                ax.scatter(
                    frame.geometry.x, frame.geometry.y,
                    s=frame["_psize"].to_numpy(dtype="float64"),
                    c=color_override, edgecolors=edge_color, linewidths=0.4,
                    alpha=alpha, zorder=zorder,
                )
                return
            for bucket, group in frame.groupby("source_bucket", sort=False):
                ax.scatter(
                    group.geometry.x, group.geometry.y,
                    s=group["_psize"].to_numpy(dtype="float64"),
                    c=plant_color_map[bucket], edgecolors=edge_color, linewidths=0.4,
                    alpha=alpha, zorder=zorder,
                )

        if highlight_mode and "is_highlighted" in styled_plants.columns:
            hp = styled_plants["is_highlighted"].to_numpy(dtype=bool)
            _scatter_plants(styled_plants[~hp], zorder=9, alpha=0.30)
            _scatter_plants(styled_plants[hp], zorder=9.3, alpha=0.95, color_override=highlight_color)
        else:
            _scatter_plants(styled_plants, zorder=9, alpha=0.85)

    if substations is not None and not substations.empty:
        sub_styles = compute_substation_styles(substations, theme, marker_scale=plant_marker_scale)
        styled_subs = substations.assign(**sub_styles)
        sub_face, sub_edge = substation_colors(theme)

        def _scatter_subs(frame: gpd.GeoDataFrame, zorder: float, alpha: float, color_override=None) -> None:
            # Square marker distinguishes substations from the round plant dots.
            if frame.empty:
                return
            ax.scatter(
                frame.geometry.x, frame.geometry.y,
                s=frame["_ssize"].to_numpy(dtype="float64"),
                marker="s", c=(color_override or sub_face), edgecolors=sub_edge,
                linewidths=0.4, alpha=alpha, zorder=zorder,
            )

        if highlight_mode and "is_highlighted" in styled_subs.columns:
            hs = styled_subs["is_highlighted"].to_numpy(dtype=bool)
            _scatter_subs(styled_subs[~hs], zorder=9.1, alpha=0.30)
            _scatter_subs(styled_subs[hs], zorder=9.4, alpha=0.95, color_override=highlight_color)
        else:
            _scatter_subs(styled_subs, zorder=9.1, alpha=0.85)

    ax.set_aspect("equal", adjustable="box")
    set_country_extent(ax, boundary, width, height, padding=padding, shift_x=shift_x, shift_y=shift_y)

    add_gradient_fade(ax, theme.fade, "bottom", zorder=10, height=fade_bottom_height, max_alpha=fade_bottom_alpha)
    add_gradient_fade(ax, theme.fade, "top", zorder=10, height=fade_top_height, max_alpha=fade_top_alpha)

    scale = min(width, height) / 12
    # Landscape posters have less vertical room, so the title at the same point
    # size occupies a larger fraction of the poster height and looks oversized.
    title_factor = 48 if height >= width else 36
    title_pt = title_size if title_size is not None else title_factor * scale
    font_main = FontProperties(family="DejaVu Sans", weight="bold", size=title_pt)
    font_sub = FontProperties(family="DejaVu Sans", weight="normal", size=15 * scale)
    font_meta = FontProperties(family="DejaVu Sans Mono", weight="normal", size=8.5 * scale)

    year = datetime.now().year
    low_kv, mid_kv, high_kv, extra_kv = voltage_tiers
    total_length_km = float(lines.geometry.length.sum()) / 1000.0
    high_voltage_length_km = float(lines.loc[lines["voltage_kv"].fillna(0) >= mid_kv].geometry.length.sum()) / 1000.0
    if subtitle is None:
        subtitle = "ELECTRICAL GRID" if include_minor_lines else "ELECTRICAL TRANSMISSION GRID"
    metadata = f"{year} · {total_length_km:,.0f}{THIN_SPACE}km of power lines"
    if high_voltage_length_km:
        metadata += f" · {high_voltage_length_km:,.0f}{THIN_SPACE}km ≥{mid_kv:g}{THIN_SPACE}kV"

    breakdown_rows: list[tuple[str, str, float]] = []
    kv = lines["voltage_kv"].astype("float64")
    seg_km = lines.geometry.length / 1000.0
    tiers = [
        (low_kv, mid_kv, theme.line_low, f"{low_kv:g}–{mid_kv:g}{THIN_SPACE}kV"),
        (mid_kv, high_kv, theme.line_mid, f"{mid_kv:g}–{high_kv:g}{THIN_SPACE}kV"),
        (high_kv, extra_kv, theme.line_high, f"{high_kv:g}–{extra_kv:g}{THIN_SPACE}kV"),
        (extra_kv, None, theme.line_extra, f"≥{extra_kv:g}{THIN_SPACE}kV"),
    ]
    for low_kv, high_kv, color, label in tiers:
        mask = kv >= low_kv
        if high_kv is not None:
            mask &= kv < high_kv
        tier_km = float(seg_km[mask].sum())
        if tier_km > 0:
            breakdown_rows.append((label, color, tier_km))

    # Per-source plant capacity breakdown for the second metadata row. Unknown
    # capacities count as 0 GW but the bucket still appears, since its plants
    # are visible on the map.
    plant_rows: list[tuple[str, str, float]] = []
    if plants is not None and not plants.empty:
        bucket_gw = plants.groupby("source_bucket")["capacity_mw"].sum(min_count=0) / 1000.0
        for bucket in PLANT_SOURCE_BUCKETS:
            if bucket in bucket_gw.index:
                gw = float(np.nan_to_num(bucket_gw[bucket]))
                plant_rows.append((bucket.upper(), plant_color_map[bucket], gw))

    # Highlight summary: how much of the grid the contribution covers. In
    # highlight mode this single row replaces the voltage/plant breakdowns.
    def _highlighted_count(frame: gpd.GeoDataFrame | None) -> int:
        if frame is None or frame.empty or "is_highlighted" not in frame.columns:
            return 0
        return int(frame["is_highlighted"].to_numpy(dtype=bool).sum())

    highlight_km = 0.0
    if highlight_mode and "is_highlighted" in lines.columns:
        hl_mask = lines["is_highlighted"].to_numpy(dtype=bool)
        highlight_km = float(lines.loc[hl_mask].geometry.length.sum()) / 1000.0
    n_hl_plants = _highlighted_count(plants) if highlight_mode else 0
    n_hl_subs = _highlighted_count(substations) if highlight_mode else 0

    ax.text(
        0.5,
        0.130,
        spaced_upper(display_country),
        transform=ax.transAxes,
        ha="center",
        va="center",
        color=theme.text,
        fontproperties=font_main,
        zorder=TEXT_ZORDER,
    )
    ax.text(
        0.5,
        0.090,
        subtitle,
        transform=ax.transAxes,
        ha="center",
        va="center",
        color=theme.subtext,
        fontproperties=font_sub,
        zorder=TEXT_ZORDER,
    )
    def _seg(text: str, color: str, alpha: float) -> TextArea:
        return TextArea(text, textprops=dict(fontproperties=font_meta, color=color, alpha=alpha))

    def _add_metadata_row(children: list[TextArea], y: float) -> None:
        packed = HPacker(children=children, align="center", sep=4.0 * scale, pad=0)
        anchored = AnchoredOffsetbox(
            loc="center",
            child=packed,
            bbox_to_anchor=(0.5, y),
            bbox_transform=ax.transAxes,
            frameon=False,
            pad=0,
            borderpad=0,
        )
        anchored.set_zorder(TEXT_ZORDER)
        ax.add_artist(anchored)

    if include_metadata and highlight_mode:
        # In highlight mode a single contribution-summary row replaces the
        # voltage/plant breakdowns, in the vivid highlight color so it ties back
        # to the highlighted features on the map.
        label = highlight_label or "CONTRIBUTIONS"
        children = [_seg(str(year), theme.subtext, 0.85)]
        children.append(_seg("·", theme.subtext, 0.85))
        children.append(_seg(label, highlight_color, 0.97))
        children.append(_seg(f"{highlight_km:,.0f}{THIN_SPACE}km", theme.subtext, 0.85))
        if n_hl_plants:
            children.append(_seg("·", theme.subtext, 0.85))
            children.append(_seg(f"{n_hl_plants:,} plants", theme.subtext, 0.85))
        if n_hl_subs:
            children.append(_seg("·", theme.subtext, 0.85))
            children.append(_seg(f"{n_hl_subs:,} substations", theme.subtext, 0.85))
        _add_metadata_row(children, 0.064)
    elif include_metadata and breakdown_rows:
        children = [_seg(str(year), theme.subtext, 0.85)]
        for label, color, tier_km in breakdown_rows:
            children.append(_seg("·", theme.subtext, 0.85))
            children.append(_seg(label, color, 0.95))
            children.append(_seg(f"{tier_km:,.0f}{THIN_SPACE}km", theme.subtext, 0.85))
        _add_metadata_row(children, 0.064)
    elif include_metadata:
        ax.text(
            0.5,
            0.064,
            metadata,
            transform=ax.transAxes,
            ha="center",
            va="center",
            color=theme.subtext,
            alpha=0.85,
            fontproperties=font_meta,
            zorder=TEXT_ZORDER,
        )

    if include_metadata and not highlight_mode and plant_rows:
        # Second breakdown row: installed capacity per plant source, placed
        # between the voltage row (0.064) and the credit line (0.018).
        children = []
        for index, (label, color, gw) in enumerate(plant_rows):
            if index:
                children.append(_seg("·", theme.subtext, 0.85))
            children.append(_seg(label, color, 0.95))
            children.append(_seg(f"{gw:,.1f}{THIN_SPACE}GW", theme.subtext, 0.85))
        _add_metadata_row(children, 0.040)

    ax.plot(
        [0.39, 0.61],
        [0.112, 0.112],
        transform=ax.transAxes,
        color=theme.text,
        linewidth=0.8 * scale,
        alpha=0.85,
        zorder=TEXT_ZORDER,
    )
    ax.text(
        0.985,
        0.018,
        "© OpenStreetMap contributors, MapYourGrid, Open Energy Transition · CC BY 4.0",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        color=theme.subtext,
        alpha=0.65,
        fontproperties=font_meta,
        zorder=TEXT_ZORDER,
    )

    if logo_image is not None:
        add_logo(fig, logo_image, width, height, logo_size_mm, logo_margin_mm, logo_alpha)

    for output_file, fmt in outputs:
        save_kwargs: dict[str, Any] = {
            "format": fmt,
            "facecolor": bg_color,
            "transparent": transparent_background,
            "bbox_inches": None,
            "pad_inches": 0,
        }
        if fmt == "png":
            save_kwargs["dpi"] = dpi

        output_file.parent.mkdir(exist_ok=True)
        fig.savefig(output_file, **save_kwargs)
        print(f"Saved poster: {output_file}")
    plt.close(fig)
