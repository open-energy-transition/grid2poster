"""Shared constants, the on-disk pickle cache, and small utilities."""

from __future__ import annotations

import hashlib
import json
import pickle
import re
import unicodedata
from importlib import resources
from pathlib import Path
from typing import Any

try:
    from tqdm.auto import tqdm
except ImportError:  # pragma: no cover - optional progress-bar dependency

    class tqdm:  # no-op stand-in supporting both iteration and manual updates
        def __init__(self, iterable=None, *args, **kwargs):
            self._iterable = iterable

        def __iter__(self):
            return iter(self._iterable or [])

        def update(self, n=1):
            pass

        def set_description(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False


CACHE_DIR = Path("cache")
POSTERS_DIR = Path("posters")
FILE_ENCODING = "utf-8"
MM_PER_INCH = 25.4

PAPER_SIZES: dict[str, tuple[float, float]] = {
    # ISO 216 A-series (portrait, width × height in mm)
    "a5": (148.0, 210.0),
    "a4": (210.0, 297.0),
    "a3": (297.0, 420.0),
    "a2": (420.0, 594.0),
    "a1": (594.0, 841.0),
    "a0": (841.0, 1189.0),
    # ANSI / North American sizes
    "letter": (215.9, 279.4),
    "legal": (215.9, 355.6),
    "tabloid": (279.4, 431.8),
}

# Lower kV bound of each voltage tier (low, mid, high, extra). A line is placed
# in the highest tier whose bound it meets; below the first bound it is treated
# as unknown/sub-transmission. Overridable per-run via --voltage-tiers.
DEFAULT_VOLTAGE_TIERS: tuple[float, float, float, float] = (60.0, 150.0, 300.0, 500.0)


def data_dir(name: str) -> Path:
    """Locate a bundled data directory (``themes``/``regions``).

    A same-named directory in the current working directory wins, so a checkout
    of this repository - or any project keeping its own ``themes/`` next to the
    posters it renders - overrides what ships inside the package.
    """
    local = Path.cwd() / name
    if local.is_dir():
        return local
    return Path(str(resources.files(__package__) / "data" / name))


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", normalized).strip("_").lower()
    return normalized or "poster"


def cache_key(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str).encode(FILE_ENCODING)
    return hashlib.sha256(raw).hexdigest()[:24]


def ensure_cache_dir() -> Path:
    """Create the on-disk cache directory on first use rather than at import."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


def cache_get(key: str) -> Any | None:
    path = CACHE_DIR / f"{key}.pkl"
    if not path.exists():
        return None
    with path.open("rb") as handle:
        return pickle.load(handle)


def cache_set(key: str, value: Any) -> None:
    path = ensure_cache_dir() / f"{key}.pkl"
    with path.open("wb") as handle:
        pickle.dump(value, handle, protocol=pickle.HIGHEST_PROTOCOL)
