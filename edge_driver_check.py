"""Detect Edge / msedgedriver version mismatches and explain how to fix them."""

import re

DRIVER_DOWNLOAD_TEMPLATE = "https://msedgedriver.microsoft.com/{version}/edgedriver_win64.zip"

_DRIVER_RE = re.compile(r"only supports Microsoft Edge version (\d+)")
_BROWSER_RE = re.compile(r"Current browser version is ([\d.]+)")


class DriverVersionMismatch(RuntimeError):
    """Raised when msedgedriver does not match the installed Edge version."""


def parse_version_mismatch(message: str | None) -> tuple[str, str] | None:
    """Return (driver_major, browser_version) if message is a version mismatch."""
    if not message:
        return None
    driver = _DRIVER_RE.search(message)
    browser = _BROWSER_RE.search(message)
    if not (driver and browser):
        return None
    return driver.group(1), browser.group(1)


def driver_download_url(browser_version: str) -> str:
    return DRIVER_DOWNLOAD_TEMPLATE.format(version=browser_version)


def format_mismatch_help(message: str | None) -> str | None:
    """Build a human-readable fix-it message, or None if not a mismatch."""
    parsed = parse_version_mismatch(message)
    if parsed is None:
        return None
    driver_major, browser_version = parsed
    url = driver_download_url(browser_version)
    return (
        f"EdgeDriver/Edge version mismatch: msedgedriver supports Edge {driver_major}, "
        f"but installed Edge is {browser_version}.\n"
        f"  1. Download the matching driver: {url}\n"
        f"  2. Stop the running msedgedriver.exe on Windows\n"
        f"  3. Replace msedgedriver.exe with the one from the zip\n"
        f"  4. Start it again: msedgedriver.exe --port=9515"
    )
