"""GridToPoster - print-ready electrical transmission grid posters from OpenStreetMap."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("grid2poster")
except PackageNotFoundError:  # pragma: no cover - not installed (e.g. run from a source tree)
    __version__ = "0.0.0"

__all__ = ["__version__"]
