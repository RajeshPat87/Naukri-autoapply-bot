import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edge_driver_check import (  # noqa: E402
    DriverVersionMismatch,
    driver_download_url,
    format_mismatch_help,
    parse_version_mismatch,
)

REAL_MESSAGE = (
    "session not created: This version of Microsoft Edge WebDriver only supports "
    "Microsoft Edge version 156\nCurrent browser version is 158.0.4370.33 with binary "
    "path C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
)


def test_parse_extracts_driver_major_and_browser_version():
    assert parse_version_mismatch(REAL_MESSAGE) == ("156", "158.0.4370.33")


def test_parse_returns_none_for_unrelated_error():
    assert parse_version_mismatch("session not created: Chrome failed to start") is None


def test_parse_handles_none_and_empty():
    assert parse_version_mismatch(None) is None
    assert parse_version_mismatch("") is None


def test_download_url_uses_exact_browser_version():
    assert driver_download_url("158.0.4370.33") == (
        "https://msedgedriver.microsoft.com/158.0.4370.33/edgedriver_win64.zip"
    )


def test_help_message_contains_versions_and_url():
    text = format_mismatch_help(REAL_MESSAGE)
    assert "156" in text
    assert "158.0.4370.33" in text
    assert driver_download_url("158.0.4370.33") in text


def test_help_message_none_for_unrelated_error():
    assert format_mismatch_help("some other failure") is None


def test_mismatch_exception_is_runtime_error():
    assert issubclass(DriverVersionMismatch, RuntimeError)
