#!/usr/bin/env python3
"""Fresh, authenticated loopback integration demo with local fixtures only."""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import secrets
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canvas_hub.assistant import AssistantService
from canvas_hub.boundary import HubAPI
from canvas_hub.paths import get_paths
from canvas_hub.services import Hub
from canvas_hub.testing.integration_controls import install_integration_controls
from canvas_hub.testing.integration_demo import seed_integration_examples
from canvas_hub.web.app import create_app
from tests.fake_llm import FakeLLM
import uvicorn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8734)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--home', type=Path, help='Optional NEW directory for keeping synthetic data; existing homes are refused')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    if args.home and args.home.exists():
        parser.error('--home must be a new path; never overwrite an existing preview or account')
    context = nullcontext(args.home) if args.home else TemporaryDirectory(prefix='canvas-hub-integration-preview-')
    with context as home:
        hub = Hub(get_paths(Path(home)))
        seed, transport = seed_integration_examples(hub, json.loads((ROOT/'tests/fixtures/observation_package_v1.json').read_text()))
        fake = FakeLLM([], answer='Synthetic integration preview: this is a local fake answer. Your tools share saved state across Overall, course and item views. No external AI or Canvas account was contacted.')
        try:
            api = HubAPI(hub)
            api.assistant = AssistantService(hub, api)
            api.assistant.update_config({'preset':'custom', 'name':'Offline integration demo', 'base_url':fake.url+'/v1',
                'model':'offline-demo', 'permissions':{'collect':False,'notes':True,'settings':False,'deadlines':True}})
            token = secrets.token_urlsafe(32)
            app = create_app(api, token=token, port=args.port)
            install_integration_controls(app, hub, seed, transport)
            args.metadata.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(args.metadata, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
            if hasattr(os, 'fchmod'): os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as out:
                json.dump({'pid':os.getpid(), 'port':args.port, 'host':'127.0.0.1', 'home':str(home),
                    'url':f'http://127.0.0.1:{args.port}/?token={token}',
                    'controls_url':f'http://127.0.0.1:{args.port}/testing/demo',
                    'synthetic':True, 'external_ai':False, 'external_network':False, **seed}, out)
            print(f'Synthetic integration preview on loopback {args.port}. Private access metadata: {args.metadata}', flush=True)
            uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')
        finally:
            fake.stop(); hub.close()


if __name__ == '__main__': main()
