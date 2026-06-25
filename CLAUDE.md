# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Grid2Poster turns OpenStreetMap `power=*` features into print-ready posters of a
country's, region's, or continent's electrical grid. It geocodes a boundary,
downloads power-line geometries inside it via Overpass, styles them by voltage,
and renders a poster (PNG/SVG/PDF) with GeoPandas + Matplotlib.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt           # osmnx, geopandas, matplotlib, numpy, pandas, shapely, tqdm

python create_grid_poster.py --country Brazil --tile-delay 30 --tile-size-km 500
python create_grid_poster.py --country Pakistan --single-query   # one Overpass query; faster for small regions
python create_grid_poster.py --list-themes
python create_grid_poster.py --country "Europe" --boundary-geojson ./regions/europe.geojson --landscape
```

There is no test suite, linter, or build step — it is a single CLI script with
sibling modules. `cairosvg` and `tqdm` are optional (SVG logos / progress bars);
the code degrades gracefully when they are absent.

## Pipeline architecture

`create_grid_poster.py` is the CLI entry point (`main()` → `parse_args` →
fetch → prepare → render). The pipeline stages live in sibling modules and run
in this order:

1. **`osm_data.py`** — boundary resolution and Overpass downloads.
   - `get_country_boundary` geocodes via Nominatim, OR builds a continent from
     the Natural Earth admin-0 dataset (Nominatim does **not** resolve continent
     names — see `CONTINENT_NAMES` / `CONTINENT_AGGREGATES`; `Global` is a
     hand-clipped union of continents). `load_boundary_from_geojson` loads a
     local boundary instead.
   - Two fetch strategies: `fetch_power_features` tiles the boundary
     (`make_query_tiles`) and downloads per tile via `_fetch_tiles` — the shared
     engine with per-tile caching, adaptive rate-limit backoff, and indefinite
     retries. `fetch_power_features_single` does it in one `poly:` Overpass query
     (`--single-query`). `fetch_power_plants` reuses `_fetch_tiles`.
2. **`prepare.py`** — OSM tag parsing + geometry prep. `parse_voltage_to_kv`,
   `parse_capacity_to_mw`, and `bucket_plant_source` are the tag normalizers.
   `prepare_lines` reprojects, clips to the boundary, explodes, and assigns
   `voltage_kv`/`sort_voltage`. `prepare_plants` collapses plant areas to marker
   points and parses capacity/source.
3. **`theming.py`** — `Theme` dataclass + `load_theme`. Styling is **vectorized**:
   `compute_line_styles` returns per-row color/linewidth/alpha arrays (so
   `render` can batch one Matplotlib call per style group, not per segment);
   `compute_plant_styles` does the same for plant markers.
4. **`render.py`** — `render_poster` composes extent, gradient fades, voltage/
   plant legend rows, logo, and writes each output format.
5. **`common.py`** — shared constants (`PAPER_SIZES`, `DEFAULT_VOLTAGE_TIERS`,
   dir paths), `slugify`, and the pickle cache (`cache_key`/`cache_get`/`cache_set`).
6. **`changesets.py`** — only used by highlight mode (`--highlight-hashtag`).
   Maps OSM changeset ids to their comment/`hashtags` tags via the OSM changeset
   API (batched ≤100, per-changeset cached) to decide which features a hashtag covers.

**Highlight mode** (`--highlight-hashtag` / `--highlight-users`) is a parallel,
metadata-aware fetch path: OSMnx strips OSM metadata, so `osm_data.py` has raw
`out meta geom;` fetchers (`fetch_power_features_meta`, `fetch_power_plants_meta`,
`fetch_substations`) that reuse the same tiled engine — `_fetch_tiles` takes a
`fetch_fn` callable so both the OSMnx and raw-meta paths share its cache/backoff/
retry. An `is_highlighted` bool is threaded fetch→prepare→render; matched features
draw vivid over a dimmed base. These fetchers use distinct `*_meta_*` cache
namespaces so they never collide with the metadata-free caches.

## Key conventions

- **Caching is central to UX.** Boundaries, full power-feature sets, AND
  individual tiles are pickled into `cache/` keyed by `cache_key(...)`. The first
  render of a region is slow; re-rendering with another theme is cheap because
  downloads are skipped. Per-tile caching means a crashed continent-scale run
  resumes where it left off. When changing fetch logic, bump the version string
  in the relevant `cache_key("...", ...)` call to invalidate stale entries.
- **Voltage tiers** (`--voltage-tiers`, default `60,150,300,500` kV) drive line
  color, weight, and legend labels. A line lands in the highest tier whose lower
  bound it meets; below the first bound it is "unknown".
- **Themes are JSON in `themes/`.** Required keys: `name`, `description`, `bg`,
  `text`, `subtext`, `boundary`, `line_unknown/low/mid/high/extra`, `fade`.
  Everything else (`lw_*` widths, `cable_color`/`cable_lw_scale`, `plant_*`
  marker colors) is optional and falls back to dataclass defaults or a
  palette-derived color (`derive_plant_colors`). The three `DEFAULT_THEMES` in
  `theming.py` are written to disk on first run if missing.
- **CRS:** data is fetched/stored in EPSG:4326 and rendered in EPSG:3857 (`--crs`).
- **zorder budget in `render.py`:** lines occupy ~2–8 (capped so a mis-tagged
  voltage can't cover the title), gradient fades sit at 10, all text/overlays at
  `TEXT_ZORDER` (100). Preserve this layering when adding overlays.
- **Output filenames** are `{slugify(country)}_grid_{theme}_{timestamp}.{fmt}`,
  written to `posters/`.

## Predefined regions

`regions/` holds multi-country boundary GeoJSONs (power pools, interconnections,
island groups) used via `--boundary-geojson`; `--country` only sets the printed
title. See the README table for what each file covers.

## Gallery contribution

The published gallery lives on the orphan `gh-pages` branch (no shared history
with `main`), built by `build_manifest.py` which exists only there. To add a
poster: render on `main`, move the PNG/SVG aside, `git checkout gh-pages`, move
it into `posters/`, run `python build_manifest.py`, and open a PR targeting
`gh-pages`. The default `--single-branch main` clone does not fetch it; run
`git fetch origin gh-pages` first.
