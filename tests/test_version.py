"""The one version: app/__init__.py → the footer and /version (which the updater checks a new install by)."""
import re

from app import __version__


def test_version_is_a_release_or_dev_string():
    assert re.fullmatch(r"\d+\.\d+\.\d+(-(dev|rc\.\d+))?", __version__)


def test_version_endpoint_and_footer_show_it(client):
    assert client.get("/version").json()["app_version"] == __version__
    page = client.get("/modules").text
    assert f"App Version: {__version__}" in page
    assert "1.3.1" not in page
