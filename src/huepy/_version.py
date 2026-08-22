"""The installed package version.

Kept in its own module so both :mod:`huepy` and :mod:`huepy.client.base` can
read it without importing each other.
"""

from importlib.metadata import PackageNotFoundError, version

PACKAGE_NAME = "huepy"


def package_version() -> str:
    """Return the installed huepy version.

    Returns:
        The version string, or ``"unknown"`` when running from a source tree
        with no installed distribution metadata.

    """
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:  # pragma: no cover - only when not installed
        return "unknown"
