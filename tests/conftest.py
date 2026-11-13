import copy
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("CANVAS_HUB_HEADLESS", "1")

from canvas_hub.paths import get_paths  # noqa: E402
from canvas_hub.services import Hub  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "observation_package_v1.json"


@pytest.fixture
def external_inspection_enabled(monkeypatch):
    """Explicit opt-in for maintaining dormant inspection code using fakes only.

    Freeze acceptance tests do NOT use this fixture. No runtime setting enables
    the feature, and ordinary collector/Assistant tests retain the shipped default.
    """
    from canvas_hub import external_inspection
    monkeypatch.setattr(external_inspection, 'ENABLED', True)


@pytest.fixture(autouse=True)
def configured_browser_executable(request, monkeypatch):
    """Use the app's browser override for UI tests as well as the collector."""
    executable = os.environ.get("CANVAS_HUB_CHROMIUM")
    if executable and request.node.get_closest_marker("browser"):
        from playwright.sync_api import BrowserType
        def configure(launch):
            def configured_launch(browser_type, *args, **kwargs):
                if browser_type.name == "chromium" and not kwargs.get("executable_path") and not kwargs.get("channel"):
                    kwargs["executable_path"] = executable
                return launch(browser_type, *args, **kwargs)
            return configured_launch

        for method in ("launch", "launch_persistent_context"):
            monkeypatch.setattr(BrowserType, method, configure(getattr(BrowserType, method)))


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


@pytest.fixture
def fake_sites():
    """Synthetic external sites: Basic sign-in, single sign-on, and a site with its own form."""
    from canvas_hub.testing.fake_sites import FakeSites
    sites = FakeSites().start()
    yield sites
    sites.stop()


@pytest.fixture
def inspector(hub, account, fixture_pkg):
    """A link inspector that may open the synthetic sites on 127.0.0.1 (the app itself never can)."""
    from canvas_hub.services.link_inspection import LinkInspector
    hub.importer.import_package(fixture_pkg)
    service = LinkInspector(hub, allow_local=True)
    service.online = lambda _aid: True
    yield service
    service.stop()


def in_sign_in_window(inspector, action, timeout=30):
    """Stand in for the user: run ``action(page)`` in the open sign-in window, on the browser's own thread."""
    import threading
    import time
    deadline = time.monotonic() + timeout
    while inspector._login is None and time.monotonic() < deadline:
        time.sleep(0.05)
    done, out = threading.Event(), {}

    def job():
        try:
            out["value"] = action(inspector._login["page"])
        except Exception as e:  # noqa: BLE001
            out["error"] = e
        finally:
            done.set()
    inspector._q.put(job)
    assert done.wait(timeout), "the sign-in window did not respond"
    if "error" in out:
        raise out["error"]
    return out.get("value")


def settled(inspector, account_id, source_id, timeout=30):
    """Wait until a link is neither being read nor waiting for a sign-in."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = inspector.view(inspector._source(account_id, source_id))
        if view["state"] == "idle":
            return view
        time.sleep(0.1)
    raise AssertionError(f"link {source_id} did not settle: {view}")
