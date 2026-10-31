"""Command line entry point: `canvas-hub <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__


def _open_browser_when_ready(url: str, host: str, port: int, timeout: float = 15) -> threading.Thread:
    """Open the UI only after the local server accepts connections."""
    def wait_and_open():
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.25):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            return
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    thread = threading.Thread(target=wait_and_open, name="canvas-hub-open-browser", daemon=True)
    thread.start()
    return thread


def _hub(args):
    from .paths import get_paths
    from .services import Hub
    return Hub(get_paths(Path(args.home) if args.home else None))


def cmd_serve(args):
    import uvicorn

    from .boundary import HubAPI
    from .collector.browser import BrowserWorker
    from .collector.runner import JobRunner
    from .web.app import create_app
    from .web.security import load_or_create_token

    if args.host not in ("127.0.0.1", "localhost"):
        print("Refusing to listen on a non-loopback address; Canvas Hub is a local, single-user app.", file=sys.stderr)
        return 2
    hub = _hub(args)
    worker = BrowserWorker(hub, headless=True if args.headless else None)
    runner = JobRunner(hub, worker)
    runner.start()
    api = HubAPI(hub, runner=runner, worker=worker)
    from .assistant import AssistantService
    api.assistant = AssistantService(hub, api, worker)
    token = load_or_create_token(hub.paths.runtime)
    app = create_app(api, token=token, port=args.port)
    url = f"http://127.0.0.1:{args.port}/?token={token}"
    print(f"Canvas Hub {__version__}")
    print(f"  data:     {hub.paths.data}")
    print(f"  browser:  {hub.paths.browser_profiles} (sign-in sessions; never exported)")
    if not runner.has_lock:
        print("  NOTE: another Canvas Hub process is running collection; this one serves stored data only.")
    print(f"\nOpen: {url}\n(keep this link private; it grants access to your local data)")
    if not args.no_open:
        _open_browser_when_ready(url, "127.0.0.1", args.port)
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        runner.stop()
        worker.stop()
        hub.close()
    return 0


def cmd_paths(args):
    hub = _hub(args)
    p = hub.paths
    print(json.dumps({"home": str(p.home), "database": str(p.db), "resources": str(p.resources),
                      "backups": str(p.backups), "browser_profiles": str(p.browser_profiles),
                      "runtime": str(p.runtime)}, indent=2))
    return 0


def cmd_status(args):
    from .boundary import HubAPI
    api = HubAPI(_hub(args))
    out = api.list_identities()
    for inst in out["instances"]:
        for a in inst["accounts"]:
            a["freshness"] = api.freshness(a["id"])
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_backup(args):
    from .services.backup import create_backup
    p = create_backup(_hub(args), Path(args.output) if args.output else None)
    print(p)
    return 0


def cmd_verify_backup(args):
    from .services.backup import verify_backup
    print(json.dumps(verify_backup(Path(args.file)), indent=2))
    return 0


def cmd_restore(args):
    from .services.backup import restore_backup
    if not args.yes:
        print("Restoring replaces the current data directory (it is moved aside, not deleted). Re-run with --yes.")
        return 1
    print(json.dumps(restore_backup(_hub(args), Path(args.file)), indent=2))
    return 0


def cmd_export(args):
    from .services.backup import export_account
    print(export_account(_hub(args), int(args.account), Path(args.output) if args.output else None))
    return 0


def cmd_import_archive(args):
    from .services.backup import import_export
    print(json.dumps(import_export(_hub(args), Path(args.file)), indent=2))
    return 0


def cmd_validate(args):
    from .boundary import HubAPI
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    print(json.dumps(HubAPI(_hub(args)).validate_observations(data), indent=2))
    return 0


def cmd_import_observations(args):
    from .boundary import HubAPI
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    res = HubAPI(_hub(args)).import_observations(data, account_id=args.account, allow_new_account=args.allow_new_account)
    print(json.dumps(res, indent=2))
    return 0


def cmd_rebuild_search(args):
    print(f"indexed {_hub(args).rebuild_search()} items")
    return 0


def cmd_clear_session(args):
    print("cleared" if _hub(args).clear_browser_session(int(args.instance)) else "no saved session")
    return 0


def cmd_delete_data(args):
    print(json.dumps(_hub(args).delete_account_data(int(args.account), confirm=args.confirm), indent=2))
    return 0


def cmd_mcp(args):
    from .mcp_server import serve_stdio
    perms = {"read"}
    if args.allow_notes or args.allow_writes:
        perms.add("notes")
    if args.allow_collection or args.allow_writes:
        perms.add("collect")
    if args.allow_settings:
        perms.add("settings")
    if args.allow_fulfil:
        perms.add("fulfil")
    serve_stdio(_hub(args), perms)
    return 0


def cmd_schema(args):
    from .observations import ObservationPackage
    text = json.dumps(ObservationPackage.model_json_schema(), indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


def cmd_fake_canvas(args):
    from .testing.fake_canvas import MFA_CODE, PASSWORD, FakeCanvas
    fc = FakeCanvas(port=args.port)
    print(f"Synthetic Canvas (test data only) at {fc.origin}")
    print(f"  sign in with username 'astudent', password '{PASSWORD}', code '{MFA_CODE}'")
    fc.httpd.serve_forever()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="canvas-hub", description="Personal local Canvas information hub")
    ap.add_argument("--home", help="data home (default: platform app-data dir or $CANVAS_HUB_HOME)")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the local app (UI, collector, job runner)")
    s.add_argument("--port", type=int, default=int(os.environ.get("CANVAS_HUB_PORT", "8730")))
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--no-open", action="store_true", help="do not open the UI in the default browser")
    s.add_argument("--headless", action="store_true", help="run the Canvas browser headless (tests only; sign-in needs a window)")
    s.set_defaults(fn=cmd_serve)

    sub.add_parser("paths", help="show data locations").set_defaults(fn=cmd_paths)
    sub.add_parser("status", help="show accounts and freshness").set_defaults(fn=cmd_status)

    s = sub.add_parser("backup", help="create a full backup zip (no browser sessions)")
    s.add_argument("--output")
    s.set_defaults(fn=cmd_backup)
    s = sub.add_parser("verify-backup", help="check a backup archive")
    s.add_argument("file")
    s.set_defaults(fn=cmd_verify_backup)
    s = sub.add_parser("restore", help="restore a backup (current data is moved aside)")
    s.add_argument("file")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("export", help="portable export of one account (observations + originals + notes)")
    s.add_argument("--account", required=True)
    s.add_argument("--output")
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("import-archive", help="import a portable export zip")
    s.add_argument("file")
    s.set_defaults(fn=cmd_import_archive)

    s = sub.add_parser("validate", help="validate an observation package JSON file")
    s.add_argument("file")
    s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("import-observations", help="import an observation package JSON file")
    s.add_argument("file")
    s.add_argument("--account", type=int)
    s.add_argument("--allow-new-account", action="store_true")
    s.set_defaults(fn=cmd_import_observations)

    sub.add_parser("rebuild-search", help="rebuild the full-text index").set_defaults(fn=cmd_rebuild_search)
    s = sub.add_parser("clear-session", help="delete the saved browser sign-in for an instance")
    s.add_argument("--instance", required=True)
    s.set_defaults(fn=cmd_clear_session)
    s = sub.add_parser("delete-data", help="delete all stored data for an account")
    s.add_argument("--account", required=True)
    s.add_argument("--confirm", required=True, help="must be DELETE")
    s.set_defaults(fn=cmd_delete_data)

    s = sub.add_parser("mcp", help="run the local MCP server on stdio (for Codex, Claude Code, other agents)")
    s.add_argument("--allow-notes", action="store_true", help="allow creating/editing local notes and to-dos")
    s.add_argument("--allow-collection", action="store_true", help="allow starting/cancelling collection jobs")
    s.add_argument("--allow-settings", action="store_true", help="allow changing the collection scope")
    s.add_argument("--allow-writes", action="store_true", help="shorthand for --allow-notes --allow-collection")
    s.add_argument("--allow-fulfil", action="store_true",
                   help="allow this agent to claim and complete requests to read external sites")
    s.set_defaults(fn=cmd_mcp)

    s = sub.add_parser("schema", help="print the observation package JSON Schema")
    s.add_argument("--output")
    s.set_defaults(fn=cmd_schema)

    s = sub.add_parser("fake-canvas", help="run the synthetic Canvas server (testing/demo)")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_fake_canvas)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.fn(args) or 0
    except Exception as e:  # noqa: BLE001
        from .util import HubError
        if isinstance(e, HubError):
            print(json.dumps(e.to_dict(), indent=2), file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
