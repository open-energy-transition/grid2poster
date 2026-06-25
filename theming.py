"""Theme definitions plus per-line and per-plant styling derived from them."""

from __future__ import annotations

import colorsys
import json
from dataclasses import dataclass
from typing import Any

import geopandas as gpd
import matplotlib.colors as mcolors
import numpy as np

from common import DEFAULT_VOLTAGE_TIERS, FILE_ENCODING, THEMES_DIR
from prepare import PLANT_SOURCE_BUCKETS

DEFAULT_THEMES: dict[str, dict[str, str]] = {
    "electric_midnight": {
        "name": "Electric Midnight",
        "description": "Deep navy background with cool transmission glow.",
        "bg": "#06111F",
        "text": "#EAF4FF",
        "subtext": "#8FB7D8",
        "boundary": "#1D3C5A",
        "line_unknown": "#355B7C",
        "line_low": "#6FA4C8",
        "line_mid": "#A7D8FF",
        "line_high": "#F6E7A7",
        "line_extra": "#FFFFFF",
        "fade": "#06111F",
    },
    "paper_grid": {
        "name": "Paper Grid",
        "description": "Warm gallery-print styling for dense networks.",
        "bg": "#F4EFE6",
        "text": "#1D1D1D",
        "subtext": "#6B625A",
        "boundary": "#D9CCBA",
        "line_unknown": "#CAB99D",
        "line_low": "#7F9A8B",
        "line_mid": "#436F75",
        "line_high": "#A65C2E",
        "line_extra": "#5B2016",
        "fade": "#F4EFE6",
    },
    "blackout": {
        "name": "Blackout",
        "description": "High-contrast black-and-white technical poster.",
        "bg": "#050505",
        "text": "#FFFFFF",
        "subtext": "#A5A5A5",
        "boundary": "#252525",
        "line_unknown": "#454545",
        "line_low": "#888888",
        "line_mid": "#D8D8D8",
        "line_high": "#FFFFFF",
        "line_extra": "#FFFFFF",
        "fade": "#050505",
    },
}


@dataclass(frozen=True)
class Theme:
    name: str
    description: str
    bg: str
    text: str
    subtext: str
    boundary: str
    line_unknown: str
    line_low: str
    line_mid: str
    line_high: str
    line_extra: str
    fade: str
    # Optional per-voltage-tier line thickness (points). When a theme omits a
    # key, the matching default below is used — so existing themes keep their
    # current look without changes.
    lw_unknown: float = 0.30
    lw_low: float = 0.48
    lw_mid: float = 0.72
    lw_high: float = 1.05
    lw_extra: float = 1.35
    lw_minor: float = 0.50
    # Optional cable (underground/submarine) styling. cable_color overrides the
    # per-voltage-tier color for cables; when None they keep their tier color.
    # cable_lw_scale multiplies the tier line width — 0.5 reproduces the prior
    # hardcoded dampening, so themes that omit these keys are unchanged.
    cable_color: str | None = None
    cable_lw_scale: float = 0.5
    # Optional power-plant marker colors, one per plant:source bucket. When a
    # theme omits a key, derive_plant_colors() synthesizes a palette-matched
    # color, so themes work without any plant keys. plant_edge overrides the
    # marker outline color (defaults to the background for a separating halo).
    plant_solar: str | None = None
    plant_wind: str | None = None
    plant_hydro: str | None = None
    plant_nuclear: str | None = None
    plant_coal: str | None = None
    plant_gas: str | None = None
    plant_oil: str | None = None
    plant_biomass: str | None = None
    plant_other: str | None = None
    plant_edge: str | None = None
    # Highlight-contributions mode. When active, all base features are dimmed
    # toward the background by highlight_dim_alpha and the matched subset is
    # redrawn in `highlight` (falling back to line_extra when omitted) at
    # highlight_lw_scale times its tier width. highlight_dim_color optionally
    # repaints the dimmed base a single neutral color; None keeps tier colors.
    highlight: str | None = None
    highlight_dim_alpha: float = 0.18
    highlight_lw_scale: float = 1.6
    highlight_dim_color: str | None = None
    # Substation markers. Theme JSONs already ship these keys; marker is the
    # fill, outline the edge. Omitted keys fall back to palette defaults in render.
    substation_fill: str | None = None
    substation_outline: str | None = None
    substation_marker: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Theme":
        required = {
            "name",
            "description",
            "bg",
            "text",
            "subtext",
            "boundary",
            "line_unknown",
            "line_low",
            "line_mid",
            "line_high",
            "line_extra",
            "fade",
        }
        missing = sorted(required.difference(raw))
        if missing:
            raise ValueError(f"Theme is missing keys: {', '.join(missing)}")
        kwargs: dict[str, Any] = {key: raw[key] for key in required}
        # Per-tier line widths and cable width scale are optional; fall back to
        # the dataclass defaults.
        for key in (
            "lw_unknown", "lw_low", "lw_mid", "lw_high", "lw_extra", "lw_minor",
            "cable_lw_scale", "highlight_dim_alpha", "highlight_lw_scale",
        ):
            if key in raw:
                kwargs[key] = float(raw[key])
        if "cable_color" in raw:
            kwargs["cable_color"] = raw["cable_color"]
        # Optional per-bucket plant marker colors; omitted keys fall back to the
        # derived palette in derive_plant_colors().
        for key in (
            "plant_solar",
            "plant_wind",
            "plant_hydro",
            "plant_nuclear",
            "plant_coal",
            "plant_gas",
            "plant_oil",
            "plant_biomass",
            "plant_other",
            "plant_edge",
            "highlight",
            "highlight_dim_color",
            "substation_fill",
            "substation_outline",
            "substation_marker",
        ):
            if key in raw:
                kwargs[key] = raw[key]
        return cls(**kwargs)


def ensure_builtin_themes() -> None:
    """Write bundled themes to disk only when they do not already exist."""
    for theme_id, data in DEFAULT_THEMES.items():
        path = THEMES_DIR / f"{theme_id}.json"
        if not path.exists():
            path.write_text(json.dumps(data, indent=2), encoding=FILE_ENCODING)


def list_themes() -> None:
    ensure_builtin_themes()
    print("Available themes:\n")
    for path in sorted(THEMES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding=FILE_ENCODING))
        except json.JSONDecodeError:
            print(f"- {path.stem}: invalid JSON")
            continue
        print(f"- {path.stem}: {data.get('name', path.stem)}")
        if data.get("description"):
            print(f"  {data['description']}")


def load_theme(theme_id: str) -> Theme:
    ensure_builtin_themes()
    path = THEMES_DIR / f"{theme_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Theme '{theme_id}' not found in {THEMES_DIR}/")
    return Theme.from_dict(json.loads(path.read_text(encoding=FILE_ENCODING)))


# Semantic hue anchors per bucket (hue degrees, saturation multiplier,
# lightness multiplier). Hues stay fixed across themes so plant types remain
# recognizable poster-to-poster; the multipliers keep coal dark and dull, solar
# bright, and "other" near-neutral regardless of the theme's vividness.
_PLANT_HUE_TABLE: dict[str, tuple[float, float, float]] = {
    "solar": (48.0, 1.10, 1.12),
    "wind": (190.0, 1.00, 1.05),
    "hydro": (215.0, 1.00, 1.00),
    "nuclear": (290.0, 0.95, 1.00),
    "coal": (30.0, 0.25, 0.72),
    "gas": (32.0, 1.05, 0.95),
    "oil": (12.0, 0.95, 0.85),
    "biomass": (130.0, 0.90, 0.95),
    "other": (0.0, 0.08, 0.90),
}


def derive_plant_colors(theme: Theme) -> dict[str, str]:
    """Per-bucket marker colors adapted to the theme's palette.

    Hue anchors are fixed (see _PLANT_HUE_TABLE) so a nuclear plant is always
    violet-ish; saturation and lightness are pulled toward the theme's line
    colors and pushed against the background so markers stay readable on both
    dark and light themes. Explicit ``plant_<bucket>`` theme keys win.
    """
    line_hls = [
        colorsys.rgb_to_hls(*mcolors.to_rgb(color))
        for color in (theme.line_unknown, theme.line_low, theme.line_mid, theme.line_high, theme.line_extra)
    ]
    mean_l = float(np.mean([hls[1] for hls in line_hls]))
    mean_s = float(np.mean([hls[2] for hls in line_hls]))
    bg_l = colorsys.rgb_to_hls(*mcolors.to_rgb(theme.bg))[1]

    # Contrast against the background, but blend toward the theme's own line
    # lightness so muted themes get muted markers rather than neon outliers.
    target_l = 0.64 if bg_l < 0.5 else 0.42
    base_l = 0.6 * target_l + 0.4 * mean_l
    base_s = float(np.clip(0.5 * mean_s + 0.35, 0.3, 0.95))

    colors: dict[str, str] = {}
    for bucket in PLANT_SOURCE_BUCKETS:
        override = getattr(theme, f"plant_{bucket}")
        if override is not None:
            colors[bucket] = override
            continue
        hue, s_mult, l_mult = _PLANT_HUE_TABLE[bucket]
        s = float(np.clip(base_s * s_mult, 0.0, 1.0))
        l = float(np.clip(base_l * l_mult, 0.08, 0.92))
        colors[bucket] = mcolors.to_hex(colorsys.hls_to_rgb(hue / 360.0, l, s))
    return colors


def compute_line_styles(
    lines: gpd.GeoDataFrame,
    theme: Theme,
    *,
    voltage_tiers: tuple[float, float, float, float] = DEFAULT_VOLTAGE_TIERS,
    highlight_mode: bool = False,
) -> dict[str, np.ndarray]:
    """Vectorized per-row (color, linewidth, alpha) for the whole frame.

    Lets render_poster batch segments into one matplotlib call per style group
    instead of one call per segment. When ``highlight_mode`` is on and an
    ``is_highlighted`` column is present, the whole frame is dimmed and the
    matched subset is repainted in the theme highlight color on top.
    """
    low_kv, mid_kv, high_kv, extra_kv = voltage_tiers
    kv = lines["voltage_kv"].astype("float64").to_numpy()
    n = len(lines)
    colors = np.full(n, theme.line_unknown, dtype=object)
    linewidths = np.full(n, theme.lw_unknown)
    alphas = np.full(n, 0.55)

    mask = kv >= low_kv
    colors[mask] = theme.line_low
    linewidths[mask] = theme.lw_low
    alphas[mask] = 0.75
    mask = kv >= mid_kv
    colors[mask] = theme.line_mid
    linewidths[mask] = theme.lw_mid
    alphas[mask] = 0.86
    mask = kv >= high_kv
    colors[mask] = theme.line_high
    linewidths[mask] = theme.lw_high
    alphas[mask] = 0.92
    mask = kv >= extra_kv
    colors[mask] = theme.line_extra
    linewidths[mask] = theme.lw_extra
    alphas[mask] = 0.95

    if "power" in lines.columns:
        power = lines["power"].to_numpy()
        minor = power == "minor_line"
        colors[minor] = theme.line_low
        linewidths[minor] = theme.lw_minor
        alphas[minor] = 0.75

        # Cables (underground/submarine) are visual context, not the headline —
        # dampen them so overhead transmission stays the story of the poster.
        # Themes may recolor them and tune the width dampening.
        is_cable = power == "cable"
        if theme.cable_color is not None:
            colors[is_cable] = theme.cable_color
        linewidths[is_cable] = linewidths[is_cable] * theme.cable_lw_scale
        alphas[is_cable] = alphas[is_cable] * 0.5

    # Highlight override wins over every tier/minor/cable rule above so the
    # contributed subset always reads as the headline of the poster.
    if highlight_mode and "is_highlighted" in lines.columns:
        hi = lines["is_highlighted"].to_numpy(dtype=bool)
        highlight_color = theme.highlight or theme.line_extra
        # Dim the entire base first...
        if theme.highlight_dim_color is not None:
            colors = np.full(n, theme.highlight_dim_color, dtype=object)
        alphas = alphas * theme.highlight_dim_alpha
        # ...then lift the matched subset back up in the vivid highlight color.
        colors[hi] = highlight_color
        linewidths[hi] = linewidths[hi] * theme.highlight_lw_scale
        alphas[hi] = 0.98

    return {"_color": colors, "_linewidth": linewidths, "_alpha": alphas}


# Plant marker sizing (matplotlib scatter ``s``, pt²). Areas scale with sqrt of
# capacity so perceived size tracks output; a ~2 GW plant hits the max size.
PLANT_CAP_REF_MW = 2000.0
PLANT_MARKER_MIN_PT2 = 12.0
PLANT_MARKER_MAX_PT2 = 320.0
PLANT_MARKER_FALLBACK_PT2 = 18.0


def compute_plant_styles(
    plants: gpd.GeoDataFrame,
    theme: Theme,
    *,
    marker_scale: float = 1.0,
    color_map: dict[str, str] | None = None,
) -> dict[str, np.ndarray]:
    """Vectorized per-plant (color, marker size) for the whole frame."""
    if color_map is None:
        color_map = derive_plant_colors(theme)

    buckets = plants["source_bucket"].to_numpy()
    colors = np.array([color_map[bucket] for bucket in buckets], dtype=object)

    capacity = plants["capacity_mw"].to_numpy(dtype="float64")
    frac = np.sqrt(np.clip(capacity, 0.0, PLANT_CAP_REF_MW) / PLANT_CAP_REF_MW)
    sizes = PLANT_MARKER_MIN_PT2 + frac * (PLANT_MARKER_MAX_PT2 - PLANT_MARKER_MIN_PT2)
    # Plants with unparseable capacity get a small fixed dot — present but
    # never mistaken for a sized marker.
    sizes = np.where(np.isnan(capacity), PLANT_MARKER_FALLBACK_PT2, sizes)
    sizes = sizes * marker_scale

    return {"_pcolor": colors, "_psize": sizes}


# Substations carry no comparable capacity tag, so they render at a single fixed
# marker size (matplotlib scatter ``s``, pt²) tunable via marker_scale.
SUBSTATION_MARKER_PT2 = 26.0


def substation_colors(theme: Theme) -> tuple[str, str]:
    """(face, edge) colors for substation markers, derived from the theme.

    Honors the optional substation_marker/substation_fill (face) and
    substation_outline (edge) theme keys; otherwise picks palette-matched
    defaults that read on both dark and light backgrounds.
    """
    face = theme.substation_marker or theme.substation_fill or theme.line_high
    edge = theme.substation_outline or theme.subtext
    return face, edge


def compute_substation_styles(
    substations: gpd.GeoDataFrame,
    theme: Theme,
    *,
    marker_scale: float = 1.0,
) -> dict[str, np.ndarray]:
    """Vectorized per-substation (face color, marker size) for the whole frame."""
    face, _edge = substation_colors(theme)
    n = len(substations)
    colors = np.full(n, face, dtype=object)
    sizes = np.full(n, SUBSTATION_MARKER_PT2 * marker_scale)
    return {"_scolor": colors, "_ssize": sizes}
