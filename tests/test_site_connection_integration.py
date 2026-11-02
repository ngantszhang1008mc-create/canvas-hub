"""Cross-feature regressions using only synthetic pages and local transports."""
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from canvas_hub.boundary import HubAPI
from canvas_hub.services.site_connection import BrowserConnection
from canvas_hub.web.app import create_app
from .test_external_sources import source


@pytest.mark.browser
def test_browser_cannot_follow_transport_redirect_outside_approved_scope():
    from playwright.sync_api import Error, sync_playwright

    hits, requests = [], []

    class Destination(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'Escaped the approved transport')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Destination)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    destination = f'http://127.0.0.1:{server.server_port}/synthetic-redirect'
    session = object.__new__(BrowserConnection)
    session.target = 'https://course.example.edu/course'
    session.domains = {'https://course.example.edu'}
    session.lock = threading.Lock()
    session.state = {'blocked_origins': []}
    session._verify_mode = True
    session._request_count = 0

    def transport(url, method, headers, body):
        requests.append((url, method))
        return 307, {'location': destination}, ['session=fixture; Secure'], b''

    session.transport = transport
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                context = browser.new_context(accept_downloads=False, service_workers='block', offline=True)
                context.route('**/*', lambda route: session._route(route, context))
                context.set_offline(False)
                page = context.new_page()
                with pytest.raises(Error, match='net::ERR_FAILED'):
                    page.goto(session.target, timeout=5000)
                assert not hits
                assert not context.cookies()
                assert requests == [(session.target, 'GET')]
                assert session.snapshot()['redirect_blocked'] is True
                assert destination not in str(session.snapshot())
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_connected_page_recheck_does_not_block_status_requests(hub, account, source, monkeypatch):
    _, sid, _ = source
    aid = account['id']
    app = create_app(HubAPI(hub), token='fixture', port=8765)
    entered, release = threading.Event(), threading.Event()

    def start(*args, **kwargs):
        assert kwargs['connected'] and kwargs['consent']
        entered.set()
        assert release.wait(5)
        return {'state': 'fixture_finished'}

    monkeypatch.setattr(app.state.source_analysis, 'start', start)
    headers = {'Authorization': 'Bearer fixture', 'X-Canvas-Hub': '1'}
    with TestClient(app, base_url='http://127.0.0.1:8765', headers=headers) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(client.post, f'/api/accounts/{aid}/external-sources/{sid}/analyze-connected',
                                  json={'token': 'fixture-consent', 'consent': True})
            try:
                assert entered.wait(2)
                status = pool.submit(client.get, f'/api/accounts/{aid}').result(timeout=2)
                assert status.status_code == 200
                assert not pending.done()
            finally:
                release.set()
            assert pending.result(timeout=2).json() == {'state': 'fixture_finished'}


@pytest.mark.browser
def test_http_source_keeps_public_analysis_panel_available(hub, account, source):
    from playwright.sync_api import expect, sync_playwright
    from .test_components_ui import serve

    service, _, reads = source
    service.register(account['id'], 'http://public.example.edu/course', course_id='4242')
    with serve(hub, []) as (base, token, _), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={'width': 380, 'height': 900})
            page.goto(f'{base}/?token={token}')
            page.goto(f'{base}/#/course/4242')
            row = page.locator('.external-source-row').filter(has_text='http://public.example.edu/course')
            row.get_by_role('button', name='Workspace actions', exact=True).click()
            row.locator('.workspace-action-icon[aria-label="Analyse link"]').click()
            panel = page.get_by_role('complementary', name='Source Agent', exact=True)
            expect(panel).to_contain_text('Public link analysis is still available')
            expect(panel.get_by_role('button', name='Approve and analyse', exact=True)).to_be_visible()
            assert not reads and not hub.requests.list(account['id'])
            panel.get_by_role('button', name='Use HTTPS version', exact=True).click()
            expect(panel.locator('h3').first).to_have_text('https://public.example.edu/course')
            expect(panel.get_by_role('button', name='Review connection', exact=True)).to_be_visible()
            assert not reads and not hub.requests.list(account['id'])
            assert not (hub.paths.browser_profiles / 'external-sites').exists()
        finally:
            browser.close()
