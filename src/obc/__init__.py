"""Own searchable catalog of the Dutch online bibliotheek (onlinebibliotheek.nl)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("online-bibliotheek-catalogus")
except PackageNotFoundError:
    __version__ = "0+unknown"
