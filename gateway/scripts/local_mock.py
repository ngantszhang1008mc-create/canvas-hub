"""Prepare a funded loopback-only MOCK fixture; never deploy or contact a model.

Run from the repository root using the Canvas Hub Python environment. Generated
credentials, invitations and ledger files stay under the ignored gateway/work/.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HOME = ROOT / "gateway" / "work" / "app-mock"
ADMIN = "synthetic_local_only_admin_000000000000000000000000000"


def private_file(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--invite", action="store_true", help="Issue a new local synthetic invitation after the mock server starts.")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    if args.invite:
        if not (HOME / "wrangler.json").exists():
            parser.error("Prepare the local fixture first.")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(f"http://127.0.0.1:{args.port}/admin/invites",
            data=b'{"expires_in_seconds":3600}', headers={"Authorization": "Bearer " + ADMIN, "Content-Type": "application/json"})
        with opener.open(request, timeout=10) as response:
            grant = json.loads(response.read(16384))
        destination = HOME / f"invitation-{uuid.UUID(grant['invite_id'])}.txt"
        private_file(destination, grant["invite"] + "\n")
        print(f"Local synthetic invitation saved to {destination}. Paste it into Assistant settings; it expires in one hour.")
        return
    from canvas_hub.agent_tools import TOOLS_BY_NAME
    source = json.loads((ROOT / "gateway" / "wrangler.jsonc").read_text())
    policy = source["env"]["mock"]["vars"]["POLICY"]
    for key in ("user_daily_microusd", "user_monthly_microusd", "global_monthly_microusd", "global_lifetime_microusd"):
        policy[key] = 100_000_000  # Synthetic accounting units, never money or a live allowance.
    policy["models"]["mock-text"]["max_input_tokens"] = 65536
    tool = TOOLS_BY_NAME["list_components"]
    policy["tools"] = {tool.name: {"description": tool.description, "parameters": tool.schema()}}
    config = {k: source[k] for k in ("compatibility_date", "durable_objects", "migrations", "observability")}
    config.update(name="canvas-hub-local-app-mock", main=str(ROOT / "gateway" / "src" / "index.ts"),
                  workers_dev=False, preview_urls=False, vars={"POLICY": policy})
    HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (HOME / "wrangler.json").exists():
        parser.error(f"Fixture already exists at {HOME}; reuse it without overwriting its ledger or credentials.")
    private_file(HOME / ".dev.vars", "ADMIN_SECRET=" + ADMIN + "\n")
    private_file(HOME / "wrangler.json", json.dumps(config, indent=2) + "\n")
    print(f"Prepared MOCK ONLY fixture at {HOME}. From gateway/, run:")
    print(f"npx wrangler dev --local --ip 127.0.0.1 --port {args.port} --config work/app-mock/wrangler.json --persist-to work/app-mock/state")


if __name__ == "__main__":
    main()
