"""Real local UI and isolated Basic transport; no external network or model calls."""
import json
import threading

import pytest

from .test_components_ui import serve
from .test_external_sources import source, HTML

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('width', [1280, 380])
def test_batch_failure_to_basic_signin_then_automatic_inspection(hub, account, source, monkeypatch, tmp_path, width):
    from playwright.sync_api import expect, sync_playwright
    from canvas_hub.assistant.providers import Turn
    from canvas_hub.services.external_reader import PublicReader, Read
    from canvas_hub.services.site_connection import BrowserConnection
    import base64

    sources, sid, _ = source
    target = sources.get(account['id'], sid)['url']
    public = sources.register(account['id'], 'https://public.example.edu/course', course_id='4242', title='Public course page')
    probes, models, connections, sessions, sent = [], [], [], [], []
    sources.reader = PublicReader(lambda u: Read(u, 401 if u == target else 200, 'text/html', b'' if u == target else HTML, auth_challenge=u == target))
    monkeypatch.setattr('canvas_hub.services.login_detection.inspect', lambda u: probes.append(u) or {
        'outcome':'login_required','auth_scheme':'basic','evidence':['http_authentication_challenge'],'course_access':'unknown'})
    class Provider:
        def complete(self, *args):
            models.append(args)
            return Turn(json.dumps({'roles':['material'],'confidence':.8,'summary':'Course materials'}))
        def close(self): pass
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider', lambda cfg: Provider())
    credential = 'Basic '+base64.b64encode(b'fixture-user:fixture-password').decode()
    def transport(url, method, headers, body):
        sent.append((url, method, headers.get('authorization')))
        if headers.get('authorization') == credential:
            return 200, {'content-type':'text/html'}, [], b'<main>'+b'Course lecture schedule and assignment information. '*12+b'<p>fixture-password</p></main>'
        return 401, {'www-authenticate':'Basic realm="fixture"','content-type':'text/html'}, [], b'Authentication failed'
    def connect(profile, url, domains, **options):
        connections.append(options)
        session=BrowserConnection(profile, url, domains, transport, **options)
        sessions.append(session)
        return session
    monkeypatch.setattr('canvas_hub.services.site_connection.BrowserConnection', connect)
    with serve(hub, []) as (base, token, _), sync_playwright() as pw:
        browser=pw.chromium.launch(); page=browser.new_page(viewport={'width':width,'height':940})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        try:
            page.goto(f'{base}/?token={token}');page.goto(f'{base}/#/course/4242')
            listing=page.locator('.external-source-list')
            listing.get_by_role('button',name='Analyse all (2)',exact=True).click()
            batch=page.get_by_role('region',name='Batch source analysis')
            assert not probes and not models and not connections
            batch.get_by_role('button',name='Approve and analyse 2 links',exact=True).click()
            row=listing.locator('.external-source-row').filter(has=page.get_by_role('link',name='Course site',exact=True))
            expect(row.get_by_role('button',name='Needs login',exact=True)).to_be_visible(timeout=10000)
            expect(batch).to_contain_text('2 / 2 completed · 1 failed · done')
            expect(listing.get_by_role('button',name='Analyse all (0)',exact=True)).to_be_disabled()
            disclosure=page.request.get(f'{base}/api/accounts/{account["id"]}/external-analysis/disclosure?course_id=4242',
                headers={'Authorization':f'Bearer {token}','X-Canvas-Hub':'1'}).json()
            assert disclosure['available'] is False  # The backend also excludes known login gates.
            assert probes==[target] and len(models)==1 and not connections
            assert sources.get(account['id'],public)['classification']
            row.get_by_role('button',name='Needs login',exact=True).click()
            panel=page.get_by_role('complementary',name='Source Agent',exact=True)
            box=panel.get_by_role('region',name='Site connection')
            expect(box.get_by_label('Site username')).to_be_focused()
            expect(box.get_by_role('button',name='Sign in and inspect')).to_be_visible()
            expect(box).to_contain_text('without a second review')
            expect(box.get_by_role('button',name='Refresh connection status')).not_to_be_visible()
            expect(box.get_by_role('button',name='Agree and open sign-in window')).to_have_count(0)
            assert not connections and not sent
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.screenshot(path=str(tmp_path/f'login-basic-{width}.png'),full_page=True)
            # One rejected attempt remains a labelled form, not an Apache error window.
            box.get_by_label('Site username').fill('fixture-user')
            box.get_by_label('Site password').fill('wrong-fixture-password')
            # This first submit launches a separate isolated Chromium profile.
            # Wait for the credential response, not a five-second DOM deadline
            # that can expire during Windows browser startup.
            with page.expect_response(lambda response: response.url.endswith('/site-connection/http_auth')
                                      and response.request.method == 'POST', timeout=20000) as rejected:
                box.get_by_label('Site password').press('Enter')
            assert rejected.value.status == 200
            expect(box.get_by_role('alert')).to_contain_text('Sign-in was not accepted')
            expect(box.get_by_label('Site password')).to_have_value('')
            box.get_by_label('Site username').fill('fixture-user')
            box.get_by_label('Site password').fill('fixture-password')
            page.wait_for_timeout(1100)  # Unchanged status must not wipe the form.
            expect(box.get_by_label('Site password')).to_have_value('fixture-password')
            box.get_by_label('Site password').press('Enter')
            expect(panel).to_contain_text('Sign-in succeeded. Inspection has started.',timeout=10000)
            assert connections==[{'headless':True}]
            assert all(url==target and method=='GET' for url,method,_ in sent)
            assert 'fixture-password' not in page.content()
            assert 'fixture-password' not in page.evaluate('JSON.stringify(localStorage)')
            expect(panel).to_contain_text('Stored observations',timeout=10000)
            expect(panel).to_contain_text('Inspection complete')
            expect(panel.get_by_role('button',name='Approve and re-inspect',exact=True)).not_to_be_visible()
            expect(panel.get_by_role('button',name='Review signed-in source',exact=True)).to_be_visible()
            page.screenshot(path=str(tmp_path/f'inspection-started-{width}.png'),full_page=True)
            assert len(models)==2 and 'fixture-password' not in json.dumps(models)
            assert sources.get(account['id'],sid)['classification']['access_mode']=='connected_local_authorized'
            assert 'fixture-password' not in json.dumps(sources.get(account['id'],sid))
            expect(row.get_by_role('button',name='Needs login',exact=True)).to_have_count(0)
            assert not errors
        finally:
            browser.close()
            # serve() asks its daemon server to stop asynchronously. Finish the
            # synthetic site browser now, before pytest closes the fixture DB
            # or exits with a Playwright/greenlet thread still alive.
            for session in sessions:
                if session.snapshot()['state'] not in ('closed','error'):
                    session.call('cancel')
                session.thread.join(timeout=5)
                assert not session.thread.is_alive()
                assert not session._basic_auth and not session._credential_redactions


def test_leaving_basic_step_while_opening_never_submits_credentials(hub, account, source, monkeypatch):
    from playwright.sync_api import expect, sync_playwright
    from .test_site_connection import FakeBrowser
    entered=threading.Event(); released=threading.Event(); credentials=[]
    class Browser(FakeBrowser):
        def __init__(self,*args,**kwargs): super().__init__(*args);entered.set()
        def snapshot(self):
            if self.state=='closed': return super().snapshot()
            return {**super().snapshot(), 'state':'login_open' if released.is_set() else 'opening',
                'http_auth':{'scheme':'basic','origin':'https://course.example.edu','request_id':'fixture'} if released.is_set() else None}
        def call(self,action,arg=None,**kwargs):
            if action=='http_auth':credentials.append(dict(arg))
            return super().call(action,arg,**kwargs)
    monkeypatch.setattr('canvas_hub.services.site_connection.BrowserConnection',Browser)
    with serve(hub,[]) as (base,token,_),sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page()
        try:
            page.goto(f'{base}/?token={token}');page.goto(f'{base}/#/course/4242')
            # A pre-existing anonymous finding; no site request is made by this route fixture.
            sid=source[1]
            page.route(f'**/external-sources/{sid}',lambda route:route.fulfill(json={**source[0].get(account['id'],sid),
                'login_check':{'state':'finished','outcome':'login_required','auth_scheme':'basic'}}))
            row=page.locator('.external-source-row')
            row.get_by_role('button',name='Workspace actions',exact=True).click()
            row.locator('.workspace-action-icon[aria-label="Analyse link"]').click()
            panel=page.get_by_role('complementary',name='Source Agent',exact=True)
            panel.get_by_text('More options and diagnostics',exact=True).click()
            panel.get_by_role('button',name='Open sign-in',exact=True).click()
            box=panel.get_by_role('region',name='Site connection')
            box.get_by_label('Site username').fill('fixture-user')
            box.get_by_label('Site password').fill('fixture-password')
            box.get_by_role('button',name='Sign in and inspect').click()
            assert entered.wait(2)
            panel.get_by_role('button',name='Close source Agent').click()
            released.set();page.wait_for_timeout(650)
            assert not credentials
            expect(panel).not_to_be_visible()
            assert page.locator('input[type=password]').evaluate_all('(inputs)=>inputs.every(x=>!x.value)')
        finally: released.set();browser.close()


def test_http_upgrade_rechecks_basic_and_generic_denial_stays_unknown(hub, account, source, monkeypatch):
    from playwright.sync_api import expect, sync_playwright
    svc, _, _ = source
    aid = account['id']
    http = svc.register(aid, 'http://course.example.edu/comp1023/', course_id='4242', title='Legacy course website')
    denied = svc.register(aid, 'https://other.example.edu/course', course_id='4242', title='Unknown access')
    probes=[]
    for sid in (http, denied):
        rid = svc.analyze(aid, sid)['analysis_request_id']
        hub.requests._set(rid, status='declined')
    def detect(url):
        probes.append(url)
        return {'outcome':'access_denied','course_access':'unknown'} if 'other.example.edu' in url else {
            'outcome':'login_required','auth_scheme':'basic','evidence':['http_authentication_challenge'],'course_access':'unknown'}
    monkeypatch.setattr('canvas_hub.services.login_detection.inspect', detect)
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider', lambda *args:pytest.fail('No provider before consent'))
    monkeypatch.setattr('canvas_hub.services.site_connection.BrowserConnection', lambda *args,**kw:pytest.fail('No connection before sign-in'))
    with serve(hub, []) as (base,token,_), sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page(viewport={'width':380,'height':900})
        try:
            page.goto(f'{base}/?token={token}');page.goto(f'{base}/#/course/4242')
            listing=page.locator('.external-source-list')
            row=listing.locator('.external-source-row').filter(has=page.get_by_role('link',name='Legacy course website',exact=True))
            row.get_by_role('button',name='Needs login',exact=True).click()
            panel=page.get_by_role('complementary',name='Source Agent',exact=True)
            box=panel.get_by_role('region',name='Site connection')
            expect(box).to_contain_text('Use a secure page to sign in')
            box.get_by_role('button',name='Use HTTPS version',exact=True).click()
            expect(box.get_by_label('Site username')).to_be_visible(timeout=10000)
            expect(box.get_by_role('button',name='Sign in and inspect')).to_be_visible()
            assert 'https://course.example.edu/comp1023/' in probes
            assert svc.get(aid,http)['url']=='http://course.example.edu/comp1023/'
            assert not svc.get(aid,http)['reads']
            panel.get_by_role('button',name='Close source Agent').click()
            row=listing.locator('.external-source-row').filter(has=page.get_by_role('link',name='Unknown access',exact=True))
            expect(row).to_contain_text('could not determine login requirement')
            expect(row.get_by_role('button',name='Needs login',exact=True)).to_have_count(0)
            row.get_by_role('button',name='Review failure',exact=True).click()
            panel.get_by_text('More options and diagnostics',exact=True).click()
            panel.get_by_role('button',name='Connect manually',exact=True).click()
            expect(box).to_contain_text('Sign-in requirement is unknown')
            expect(box.get_by_label('Site password')).to_have_count(0)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        finally: browser.close()
