#!/usr/bin/env python3
"""Scan posters/ and write posters/manifest.json for the gallery site.

Filename convention written by create_grid_poster.py:
    {region}_grid_{theme}_{YYYYMMDD}_{HHMMSS}.{png|svg|pdf}

Both region and theme can themselves contain underscores, and a handful of
themes contain the literal token "grid" (e.g. ``paper_grid``, ``aurora_grid``).
We resolve the ambiguity by splitting on the FIRST occurrence of ``_grid_``:
everything before it is the region, and everything after it (minus the
``_YYYYMMDD_HHMMSS`` suffix) is the theme.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

POSTERS_DIR = Path(__file__).parent / "posters"
TIMESTAMP_RE = re.compile(r"_(\d{8})_(\d{6})$")

# Tokens that should not be plain Title Case in display strings.
ACRONYMS = {"hdr": "HDR", "uk": "UK", "us": "US", "usa": "USA"}
VINTERP_SUFFIX = "_vinterp"


def parse_stem(stem: str) -> tuple[str, str, str] | None:
    """Return (region, theme, timestamp) or None if the name does not match."""
    sep = "_grid_"
    idx = stem.find(sep)
    if idx == -1:
        return None
    region = stem[:idx]
    rest = stem[idx + len(sep):]
    m = TIMESTAMP_RE.search(rest)
    if not m:
        return None
    theme = rest[: m.start()]
    timestamp = f"{m.group(1)}_{m.group(2)}"
    if not region or not theme:
        return None
    return region, theme, timestamp


def pretty(token: str) -> str:
    parts = token.split("_")
    return " ".join(ACRONYMS.get(p, p.capitalize()) for p in parts)


def build() -> list[dict]:
    by_key: dict[tuple[str, str, str], dict] = {}
    for path in sorted(POSTERS_DIR.iterdir()):
        if path.suffix.lower() not in {".png", ".svg"}:
            continue
        parsed = parse_stem(path.stem)
        if parsed is None:
            print(f"skipping unrecognised filename: {path.name}")
            continue
        region, theme, timestamp = parsed
        vinterp = theme.endswith(VINTERP_SUFFIX)
        theme_base = theme[: -len(VINTERP_SUFFIX)] if vinterp else theme
        key = (region, theme, timestamp)
        entry = by_key.setdefault(
            key,
            {
                "id": f"{region}_grid_{theme}_{timestamp}",
                "region": region,
                "theme": theme_base,
                "voltage_interpolation": vinterp,
                "region_display": pretty(region),
                "theme_display": pretty(theme_base),
                "timestamp": timestamp,
                "png": None,
                "svg": None,
            },
        )
        entry[path.suffix.lower().lstrip(".")] = path.name

    # Newest first, then alphabetical.
    items = sorted(
        by_key.values(),
        key=lambda e: (e["timestamp"], e["region"], e["theme"]),
        reverse=True,
    )
    return items


def main() -> None:
    items = build()
    out = POSTERS_DIR / "manifest.json"
    out.write_text(json.dumps({"posters": items}, indent=2) + "\n")
    print(f"wrote {out} ({len(items)} posters)")


if __name__ == "__main__":
    main()
