#!/usr/bin/env python3
"""Try external-link inspection without any real account.

Starts three made-up sites (a course website behind a username/password
prompt, a grader behind a university-style single sign-on, and a forum with
its own sign-in form) and a throwaway Canvas Hub that links to them. Nothing
leaves this computer and nothing is saved after you stop it.
"""
import argparse
import json
import secrets
import sys
import webbrowser
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from canvas_hub.boundary import HubAPI  # noqa: E402
from canvas_hub.paths import get_paths  # noqa: E402
from canvas_hub.services import Hub  # noqa: E402
from canvas_hub.services.link_inspection import LinkInspector  # noqa: E402
from canvas_hub.testing import fake_sites  # noqa: E402
from canvas_hub.testing.link_demo import add_links  # noqa: E402
from canvas_hub.web.app import create_app  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8736)
    parser.add_argument("--no-open", action="store_true", help="do not open the page in your browser")
    args = parser.parse_args()
    sites = fake_sites.FakeSites().start()
    with TemporaryDirectory(prefix="canvas-hub-links-") as home:
        hub = Hub(get_paths(Path(home)))
        pkg = json.loads((ROOT / "tests/fixtures/observation_package_v1.json").read_text())
        instance = hub.identity.add_instance(pkg["identity"]["origin"])
        account = hub.identity.upsert_verified_account(instance["id"], {
            "canvas_user_id": "100001", "display_name": "Link demo (made-up data)", "time_zone": "UTC"})
        hub.importer.import_package(pkg)
        add_links(hub, account["id"], sites)
        api = HubAPI(hub)
        # The made-up sites live on this computer, so this demo alone may open local addresses.
        api.link_inspector = LinkInspector(hub, allow_local=True)
        api.link_inspector.online = lambda _aid: True
        token = secrets.token_urlsafe(32)
        app = create_app(api, token=token, port=args.port)
        url = f"http://127.0.0.1:{args.port}/?token={token}#/links"
        print("External-link demo with made-up sites. Open:\n  " + url)
        print("\nSign-in details for the made-up sites:")
        print(f"  Course website   username {fake_sites.BASIC_USER!r}   password {fake_sites.BASIC_PASSWORD!r}")
        print(f"  Grader (SSO)     username {fake_sites.SSO_USER!r}   password {fake_sites.SSO_PASSWORD!r}   code {fake_sites.SSO_CODE}")
        print(f"  Forum            e-mail {fake_sites.FORUM_EMAIL!r}   password {fake_sites.FORUM_PASSWORD!r}")
        print("\nCtrl-C stops the demo and deletes its data.")
        if not args.no_open:
            webbrowser.open(url)
        try:
            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
        finally:
            sites.stop()
            hub.close()


if __name__ == "__main__":
    main()
