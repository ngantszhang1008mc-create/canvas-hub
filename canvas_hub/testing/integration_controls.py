"""Optional synthetic controls installed by preview-integration.py, never production."""
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse

from ..util import InvalidInput
from .integration_demo import propose_fixture_adapter

ASSETS = Path(__file__).with_name('integration_preview')


def install_integration_controls(app, hub, seed, transport):
    aid, sid = seed['aid'], seed['source']

    @app.get('/testing/demo')
    def demo_page():
        return FileResponse(ASSETS / 'index.html', media_type='text/html')

    @app.get('/testing/demo.js')
    def demo_script():
        return FileResponse(ASSETS / 'demo.js', media_type='text/javascript')

    @app.get('/testing/demo.css')
    def demo_style():
        return FileResponse(ASSETS / 'demo.css', media_type='text/css')

    @app.get('/api/testing/demo')
    def status():
        return {'synthetic': True, 'external_network': False, 'external_ai': False,
                'scenario': transport.scenario, 'scenarios': transport.scenarios, 'reads': len(transport.calls),
                'source_status': hub.external_sources.get(aid, sid)['status'], **seed}

    @app.post('/api/testing/scenario')
    async def scenario(request: Request):
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {'scenario'}:
            raise InvalidInput('Provide only a synthetic scenario')
        transport.select(data['scenario'])
        return status()

    @app.post('/api/testing/refresh')
    def refresh():
        hub.external_sources.refresh(aid, sid)
        return status()

    @app.post('/api/testing/propose')
    def propose():
        propose_fixture_adapter(hub, aid, sid, transport, reanalyze=True)
        return status()
