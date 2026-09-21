"""Synchronize PLMlatex documents with Moodle course resources."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version('plm-moodle-sync')
except PackageNotFoundError:
    # An uninstalled source checkout has no package metadata yet.
    __version__ = '0+unknown'
