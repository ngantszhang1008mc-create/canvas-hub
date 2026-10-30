import copy
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("CANVAS_HUB_HEADLESS", "1")

from canvas_hub.paths import get_paths  # noqa: E402
from canvas_hub.services import Hub  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "observation_package_v1.json"


@pytest.fixture(autouse=True)
def configured_browser_executable(request, monkeypatch):
    """Use the app's browser override for UI tests as well as the collector."""
    executable = os.environ.get("CANVAS_HUB_CHROMIUM")
    if executable and request.node.get_closest_marker("browser"):
        from playwright.sync_api import BrowserType
        launch = BrowserType.launch

        def configured_launch(browser_type, *args, **kwargs):
            if browser_type.name == "chromium" and not kwargs.get("executable_path") and not kwargs.get("channel"):
                kwargs["executable_path"] = executable
            return launch(browser_type, *args, **kwargs)

        monkeypatch.setattr(BrowserType, "launch", configured_launch)


@pytest.fixture
def hub(tmp_path):
    h = Hub(get_paths(tmp_path / "home"))
    yield h
    h.close()


@pytest.fixture
def fixture_pkg():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def account(hub, fixture_pkg):
    inst = hub.identity.add_instance(fixture_pkg["identity"]["origin"])
    return hub.identity.upsert_verified_account(inst["id"], {"canvas_user_id": "100001", "display_name": "Sample Student",
                                                             "time_zone": "Asia/Hong_Kong"})


def variant(pkg, package_id, observed_at, mutate=None):
    """Copy of a package as a later observation (new id, new observed_at)."""
    p = copy.deepcopy(pkg)
    p["package_id"] = package_id
    for it in p["items"]:
        it["observed_at"] = observed_at
    for c in p["coverage"]:
        c["observed_at"] = observed_at
    if mutate:
        mutate(p)
    return p
