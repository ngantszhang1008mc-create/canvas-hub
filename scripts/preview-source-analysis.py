#!/usr/bin/env python3
"""Isolated source-analysis preview. Local fixtures and fake provider only."""
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
from canvas_hub.services.external_reader import PublicReader, Read
from canvas_hub.util import InvalidInput
from canvas_hub.web.app import create_app
from tests.fake_llm import FakeLLM
import uvicorn


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8735)
    parser.add_argument('--metadata',type=Path,required=True)
    args=parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    urls=['https://course.example.edu/synthetic/notes','https://course.example.edu/synthetic/schedule']
    with TemporaryDirectory(prefix='canvas-source-preview-') as home:
        hub=Hub(get_paths(Path(home)))
        pkg=json.loads((ROOT/'tests/fixtures/observation_package_v1.json').read_text())
        instance=hub.identity.add_instance(pkg['identity']['origin'])
        hub.identity.upsert_verified_account(instance['id'],{'canvas_user_id':'100001','display_name':'Source demo · synthetic','time_zone':'UTC'})
        hub.importer.import_package(pkg)
        aid=hub.db.one('SELECT id FROM accounts')['id']
        for url in urls:
            hub.external_sources.register(aid,url,course_id='4242',title='Synthetic '+url.rsplit('/',1)[1])
        def transport(url):
            if url not in urls:
                raise InvalidInput('Only the explicit synthetic fixture URLs are available')
            return Read(url,200,'text/html',b'<h1>Course material</h1><p>Read chapter 2 before the next class. Synthetic preview only.</p>')
        hub.external_sources.reader=PublicReader(transport)
        fake=FakeLLM([],answer=json.dumps({'roles':['material'],'confidence':0.9,'summary':'Synthetic public course material, read by the built-in Assistant.'}))
        try:
            api=HubAPI(hub);api.assistant=AssistantService(hub,api)
            api.assistant.update_config({'preset':'custom','name':'Local synthetic source demo','base_url':fake.url+'/v1','model':'fake-source-model'})
            token=secrets.token_urlsafe(32)
            app=create_app(api,token=token,port=args.port)
            args.metadata.parent.mkdir(parents=True,exist_ok=True)
            fd=os.open(args.metadata,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as out:
                json.dump({'url':f'http://127.0.0.1:{args.port}/?token={token}#/course/4242','synthetic':True,'home':home},out)
            print(f'Local source-analysis preview on port {args.port}. Private launch metadata: {args.metadata}',flush=True)
            uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')
        finally:
            fake.stop();hub.close()


if __name__=='__main__':main()
