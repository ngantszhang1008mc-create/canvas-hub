#!/usr/bin/env python3
"""Separate synthetic widget demo; never reuses the active Canvas Hub home."""
import argparse
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
from canvas_hub.testing.widget_demo import seed_widget_examples
from canvas_hub.web.app import create_app
from tests.fake_llm import FakeLLM
import uvicorn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8733)
    parser.add_argument('--metadata', type=Path, required=True)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    with TemporaryDirectory(prefix='canvas-hub-widget-preview-') as home:
        hub = Hub(get_paths(Path(home)))
        seed = seed_widget_examples(hub, json.loads((ROOT/'tests/fixtures/observation_package_v1.json').read_text()))
        fake = FakeLLM([], answer='Synthetic widget preview. Your saved checklist and score calculator share the same state across Overall and course tools. No external AI was contacted.')
        try:
            api = HubAPI(hub)
            api.assistant = AssistantService(hub, api)
            api.assistant.update_config({'preset':'custom', 'name':'Offline widget demo', 'base_url':fake.url+'/v1',
                'model':'offline-demo', 'permissions':{'collect':False,'notes':True,'settings':False,'deadlines':True}})
            token = secrets.token_urlsafe(32)
            args.metadata.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(args.metadata, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
            if hasattr(os, 'fchmod'):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as out:
                json.dump({'pid':os.getpid(), 'port':args.port, 'url':f'http://127.0.0.1:{args.port}/?token={token}',
                           'synthetic':True, 'external_ai':False, **seed}, out)
            print(f'Synthetic widget preview on loopback port {args.port}. Private launch URL saved in {args.metadata}', flush=True)
            uvicorn.run(create_app(api,token=token,port=args.port),host='127.0.0.1',port=args.port,log_level='warning')
        finally:
            fake.stop()
            hub.close()


if __name__ == '__main__':
    main()
