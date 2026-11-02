#!/usr/bin/env python3
"""Authenticated loopback demo with temporary synthetic data and a local fake AI.

From the source checkout: uv run python scripts/preview-demo.py --port 8731
The private launch URL is written to --metadata; it is not production hosting.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canvas_hub.paths import get_paths
from canvas_hub.services import Hub
from canvas_hub.boundary import HubAPI
from canvas_hub.web.app import create_app
from canvas_hub.assistant import AssistantService
from tests.fake_llm import FakeLLM
import uvicorn


def seed(hub):
    package = json.loads((ROOT / 'tests/fixtures/observation_package_v1.json').read_text())
    due = datetime.now(timezone.utc) + timedelta(days=2)
    for item in package['items']:
        if item['type'] == 'assignment' and item['canvas_id'] == '7001':
            item['fields']['due_at'].update(iso=due.isoformat(), raw='Demo deadline in two days')
    instance = hub.identity.add_instance(package['identity']['origin'])
    account = hub.identity.upsert_verified_account(instance['id'], {
        'canvas_user_id': '100001', 'display_name': 'Demo Student · synthetic data', 'time_zone': 'Asia/Hong_Kong'})
    hub.importer.import_package(package)
    aid = account['id']
    iid = hub.db.one("SELECT id FROM items WHERE account_id=? AND item_type='assignment' AND canvas_id='7001'", (aid,))['id']
    checklist = hub.components.create(aid, 'checklist', {
        'title': 'Proposal checklist', 'sections': [{'id': 'work', 'rows': [
            {'id': 'read', 'label': 'Read the brief', 'item_id': iid},
            {'id': 'draft', 'label': 'Draft the proposal', 'item_id': iid}]}]}, item_id=iid)
    hub.components.set_state(aid, checklist['id'], {'done': {'read': True}})
    hub.workspace.save(aid, {'item_id': iid, 'component_id': checklist['id'], 'step_id': 'draft',
                            'next_action': 'Draft the proposal', 'view': 'requirements'})
    hub.planner.add_class(aid, title='Demo lecture', weekday=1, start='10:00', end='11:00',
                          course_id='4242', location='Synthetic classroom')
    hub.annotations.create(aid, kind='task', title='Book a study room', course_id='4242')
    hub.annotations.create(aid, kind='note', title='Offline demonstration',
        body='All coursework here is synthetic. The Agent is a local fake responder. No real Canvas account or external AI service is used.')
    return aid, iid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8731)
    parser.add_argument('--metadata', type=Path, default=Path('/tmp/canvas-hub-demo.json'))
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    with TemporaryDirectory(prefix='canvas-hub-synthetic-preview-') as home:
        hub = Hub(get_paths(Path(home)))
        aid, iid = seed(hub)
        fake = FakeLLM([], answer='Offline demo: no external AI was contacted. This preview uses synthetic coursework. You can tick checklist steps, save your next action, switch Tasks and Schedule, and return to this assignment conversation.')
        try:
            api = HubAPI(hub)
            api.assistant = AssistantService(hub, api)
            api.assistant.update_config({'preset': 'custom', 'name': 'Offline demo · no real AI',
                'base_url': fake.url + '/v1', 'model': 'offline-demo',
                'permissions': {'collect': False, 'notes': True, 'settings': False, 'deadlines': True}})
            token = secrets.token_urlsafe(32)
            args.metadata.parent.mkdir(parents=True, exist_ok=True)
            # The metadata contains a private demo-only access token; never publish it in a PR.
            fd = os.open(args.metadata, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as output:
                json.dump({'pid': os.getpid(), 'port': args.port, 'host': '127.0.0.1', 'token': token,
                    'url': f'http://127.0.0.1:{args.port}/?token={token}', 'account_id': aid,
                    'assignment_id': iid, 'synthetic': True, 'external_ai': False}, output)
            print(f'Synthetic authenticated preview on loopback port {args.port}. Private launch URL: {args.metadata}', flush=True)
            uvicorn.run(create_app(api, token=token, port=args.port), host='127.0.0.1', port=args.port, log_level='warning')
        finally:
            fake.stop()
            hub.close()


if __name__ == '__main__':
    main()
