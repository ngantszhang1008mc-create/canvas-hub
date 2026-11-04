"""Real browser workflow; the connected Agent and network transport are local fakes."""
import os
import json
from pathlib import Path

import pytest

from .test_components_ui import serve
from .test_external_sources import source, classify, SPEC
from .test_source_analysis import RESULT
from .test_codex import fake_codex

pytestmark=pytest.mark.browser


@pytest.mark.parametrize('outcome,scheme,message,button', [
    ('login_required','basic','This page requires sign-in','Needs login'),
    ('unknown',None,'Could not determine whether this page needs sign-in','Review failure'),
    ('access_denied',None,'Could not determine whether sign-in would help','Review failure'),
])
def test_failed_read_automatically_offers_login_or_manual_connection(hub, account, source, monkeypatch, outcome, scheme, message, button):
    from playwright.sync_api import expect, sync_playwright
    from canvas_hub.services.external_reader import PublicReader, Read
    sources, sid, _ = source
    url = sources.get(account['id'], sid)['url']
    sources.reader = PublicReader(lambda u: Read(u, 401, 'text/html', b'', auth_challenge=True))
    probes, models = [], []
    monkeypatch.setattr('canvas_hub.services.login_detection.inspect', lambda u: probes.append(u) or {
        'outcome':outcome,'auth_scheme':scheme,'evidence':[], 'course_access':'unknown'})
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider', lambda cfg: models.append(cfg))
    with serve(hub, []) as (base, token, _), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(f'{base}/?token={token}'); page.goto(f'{base}/#/course/4242')
            row = page.locator('.external-source-row')
            row.get_by_role('button', name='Workspace actions', exact=True).click()
            row.locator('.workspace-action-icon[aria-label="Analyse link"]').click()
            panel = page.get_by_role('complementary', name='Source Agent', exact=True)
            assert not probes
            panel.get_by_role('button', name='Approve and analyse', exact=True).click()
            expect(panel).to_contain_text(message, timeout=10000)
            expect(row.get_by_role('button', name=button, exact=True)).to_be_visible()
            if outcome!='login_required':
                expect(row.get_by_role('button', name='Needs login', exact=True)).to_have_count(0)
                panel.get_by_text('More options and diagnostics',exact=True).click()
                expect(panel.get_by_role('button',name='Connect manually',exact=True)).to_be_visible()
            assert probes == [url] and not models
            page.reload()
            row.get_by_role('button', name=button, exact=True).click()
            if scheme=='basic':
                # Verify the actionable Basic form and advance-send consent,
                # not a technical phrase removed from the streamlined copy.
                expect(panel.get_by_label('Site username',exact=True)).to_be_visible()
                expect(panel.get_by_label('Site password',exact=True)).to_be_visible()
                expect(panel.get_by_role('button',name='Sign in and inspect',exact=True)).to_be_visible()
                expect(panel).to_contain_text('without a second review')
                expect(panel.get_by_role('button',name='Agree and open sign-in window',exact=True)).to_have_count(0)
            else:
                expect(panel).to_contain_text(message)
            assert probes == [url] and not models
        finally:
            browser.close()


def test_failed_inspection_can_be_retried_and_reinspected(hub, account, source, monkeypatch):
    from playwright.sync_api import expect, sync_playwright
    from canvas_hub.assistant.providers import Turn
    calls = []
    class Provider:
        def complete(self, *args):
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError('temporary provider failure')
            return Turn(json.dumps(RESULT))
        def close(self):
            pass
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider', lambda cfg: Provider())
    svc, sid, reads = source
    with serve(hub, []) as (base, token, api), sync_playwright() as p:
        browser = p.chromium.launch(); page = browser.new_page()
        page.goto(f'{base}/?token={token}'); page.goto(f'{base}/#/course/4242')
        listing = page.locator('.external-source-list')
        listing.get_by_role('button', name='Workspace actions', exact=True).click()
        listing.locator('.workspace-action-icon[aria-label="Analyse link"]').click()
        panel = page.get_by_role('complementary', name='Source Agent', exact=True)
        panel.get_by_role('button', name='Approve and analyse', exact=True).click()
        expect(panel).to_contain_text('Latest inspection failed', timeout=15000)
        expect(panel.get_by_role('button', name='Approve and re-inspect', exact=True)).to_be_visible()
        page.reload()
        listing = page.locator('.external-source-list')
        expect(listing.get_by_role('button', name='Analyse all (1)', exact=True)).to_be_visible()
        listing.get_by_role('button', name='Review failure', exact=True).click()
        expect(panel).to_contain_text('Latest inspection failed')
        assert len(calls) == 1
        panel.get_by_role('button', name='Approve and re-inspect', exact=True).click()
        expect(panel.get_by_role('button', name='Approve, test and enable')).to_be_visible(timeout=15000)
        assert len(calls) == 2 and svc.get(account['id'], sid)['request']['status'] == 'done'
        page.reload()
        listing = page.locator('.external-source-list')
        expect(listing.get_by_role('button', name='Analyse all (0)', exact=True)).to_be_disabled()
        listing.get_by_role('button', name='Re-inspect', exact=True).click()
        expect(panel.get_by_role('button', name='Approve and re-inspect', exact=True)).to_be_visible()
        assert len(calls) == 2
        browser.close()


def test_batch_discloses_failed_retry_and_runs_only_after_approval(hub, account, source, monkeypatch):
    from playwright.sync_api import expect, sync_playwright
    from canvas_hub.assistant.providers import Turn
    calls = []
    class Provider:
        def complete(self, *args):
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError('temporary failure')
            return Turn(json.dumps(RESULT))
        def close(self):
            pass
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider', lambda cfg: Provider())
    with serve(hub, []) as (base, token, _), sync_playwright() as p:
        browser = p.chromium.launch(); page = browser.new_page()
        page.goto(f'{base}/?token={token}'); page.goto(f'{base}/#/course/4242')
        listing = page.locator('.external-source-list')
        listing.get_by_role('button', name='Workspace actions', exact=True).click()
        listing.locator('.workspace-action-icon[aria-label="Analyse link"]').click()
        panel = page.get_by_role('complementary', name='Source Agent', exact=True)
        panel.get_by_role('button', name='Approve and analyse', exact=True).click()
        expect(panel).to_contain_text('Latest inspection failed', timeout=15000)
        panel.get_by_role('button', name='Close source Agent').click()
        page.reload()
        listing.get_by_role('button', name='Analyse all (1)', exact=True).click()
        batch = page.get_by_role('region', name='Batch source analysis')
        expect(batch).to_contain_text('New: 0; previously failed and to be retried: 1')
        assert len(calls) == 1
        batch.get_by_role('button', name='Approve and analyse 1 links', exact=True).click()
        expect(batch).to_contain_text('1 / 1 completed · 0 failed · done', timeout=15000)
        assert len(calls) == 2
        browser.close()


def test_codex_link_analysis_disclosure_and_approval(hub, account, source, fake_codex):
    from playwright.sync_api import expect, sync_playwright
    setup, messages = fake_codex
    setup(reply=json.dumps(RESULT))
    svc, sid, reads = source
    with serve(hub, []) as (base, token, api), sync_playwright() as p:
        api.assistant.update_config({'preset': 'codex', 'model': '', 'name': 'Codex fixture'})
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f'{base}/?token={token}')
        page.goto(f'{base}/#/course/4242')
        listing = page.locator('.external-source-list')
        listing.get_by_role('button', name='Workspace actions', exact=True).click()
        listing.locator('.workspace-action-icon[aria-label="Analyse link"]').click()
        panel = page.get_by_role('complementary', name='Source Agent', exact=True)
        expect(panel).to_contain_text('Codex CLI (OpenAI)')
        expect(panel).to_contain_text('Provider default')
        assert not reads and not messages()
        panel.get_by_role('button', name='Approve and analyse', exact=True).click()
        expect(panel.get_by_role('button', name='Approve, test and enable')).to_be_visible(timeout=10000)
        assert len(reads) == 1 and svc.get(account['id'], sid)['request']['status'] == 'done'
        browser.close()


@pytest.mark.parametrize('executor',['builtin','mcp'])
@pytest.mark.parametrize('theme',['light','dark'])
@pytest.mark.parametrize('width',[1280,380])
def test_one_click_analysis_review_approval_and_cached_reload(hub,account,source,theme,width,tmp_path,executor):
    from playwright.sync_api import expect,sync_playwright
    svc,sid,calls=source;aid=account['id']
    with serve(hub,[],answer=json.dumps({**RESULT,'adapter_spec':SPEC})) as (base,token,_),sync_playwright() as p:
        browser=p.chromium.launch()
        page=browser.new_page(viewport={'width':width,'height':900})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(f'{base}/?token={token}')
        page.get_by_role('button',name=theme.title(),exact=True).click()
        page.goto(f'{base}/#/course/4242')
        listing=page.locator('.external-source-list');expect(listing).to_be_visible()
        expect(listing).to_contain_text('unclassified')
        listing.get_by_role('button',name='Workspace actions',exact=True).click()
        listing.locator('.workspace-action-icon[aria-label="Analyse link"]').click()
        panel=page.get_by_role('complementary',name='Source Agent',exact=True)
        expect(panel).to_be_visible();expect(panel).to_contain_text('This uses your API quota')
        assert not hub.requests.list(aid) and not calls
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        if executor=='builtin':
            panel.get_by_role('button',name='Approve and analyse',exact=True).click()
        else:
            panel.get_by_text('More options and diagnostics',exact=True).click()
            panel.get_by_role('button',name='Use connected Agent instead',exact=True).click()
            expect(panel).to_contain_text('Request #')
            classify(svc,aid,sid)
        approve=panel.get_by_role('button',name='Approve, test and enable',exact=True)
        expect(approve).to_be_visible(timeout=10000)
        expect(panel).to_contain_text('public pages only')
        expect(panel).to_contain_text('SHA-256')
        # Long source URLs, exact spec and evidence remain inside the 380px layout.
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        artifact=Path(os.environ.get('CANVAS_HUB_SOURCE_SCREENSHOTS',str(tmp_path/'external-source-evidence')))
        artifact.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(artifact/f'proposal-{executor}-{theme}-{width}.png'),full_page=True)
        approve.click()
        expect(panel.get_by_role('button',name='Refresh without Agent',exact=True)).to_be_visible(timeout=10000)
        expect(panel).to_contain_text('Deadline conflicts')
        assert len(calls)==(2 if executor=='builtin' else 1) and svc.get(aid,sid)['enabled_revision']==1
        panel.get_by_role('button',name='Refresh without Agent',exact=True).click()
        expect(panel).to_contain_text('Last successful adapter read')
        assert len(hub.requests.list(aid))==1
        panel.get_by_role('button',name='Close source Agent',exact=True).click()
        page.reload()
        listing=page.locator('.external-source-list');expect(listing).to_contain_text('enabled')
        listing.get_by_role('button',name='Workspace actions',exact=True).click()
        listing.locator('.workspace-action-icon[aria-label="Review source"]').click()
        expect(panel).to_contain_text('course_website')
        assert len(hub.requests.list(aid))==1
        page.screenshot(path=str(artifact/f'enabled-{executor}-{theme}-{width}.png'),full_page=True)
        stored=next(r for r in svc.get(aid,sid)['records'] if r['item_type']=='external_link')
        page.goto(f"{base}/#/item/{stored['id']}")
        expect(page.get_by_role('heading',name='Content read by your local adapter',exact=True)).to_be_visible()
        assert not errors,errors
        browser.close()


@pytest.mark.parametrize('width',[1280,380])
def test_visible_batch_entry_nudge_dismiss_new_links_and_scoped_consent(hub,account,source,width,tmp_path):
    from playwright.sync_api import expect,sync_playwright
    svc,sid,calls=source;aid=account['id']
    from .test_external_sources import URL
    second=svc.register(aid,URL+'&page=2',course_id='4242')
    unrelated=svc.register(aid,'https://elsewhere.example.edu/')
    answer=json.dumps({k:v for k,v in RESULT.items() if k!='adapter_spec'})
    with serve(hub,[],answer=answer) as (base,token,_),sync_playwright() as p:
        browser=p.chromium.launch();page=browser.new_page(viewport={'width':width,'height':900})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(f'{base}/?token={token}');page.goto(f'{base}/#/course/4242')
        listing=page.locator('.external-source-list')
        nudge=page.get_by_role('complementary',name='Uninspected external links')
        expect(nudge).to_be_visible();expect(nudge).to_contain_text('2 external links in this course')
        expect(listing.get_by_role('button',name='Analyse all (2)',exact=True)).to_be_visible()
        assert not calls and not hub.requests.list(aid)
        artifact=Path(os.environ.get('CANVAS_HUB_SOURCE_SCREENSHOTS',str(tmp_path/'evidence')));artifact.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(artifact/f'batch-nudge-{width}.png'),full_page=True)
        nudge.get_by_role('button',name='Dismiss link reminder').click()
        page.reload();expect(listing).to_be_visible();expect(nudge).to_have_count(0)
        third=svc.register(aid,URL+'&page=3',course_id='4242')
        page.reload();expect(nudge).to_contain_text('3 external links')
        nudge.get_by_role('button',name='View links',exact=True).click();expect(nudge).to_have_count(0)
        button=listing.get_by_role('button',name='Analyse all (3)',exact=True)
        button.focus();page.keyboard.press('Enter')
        batch=page.get_by_role('region',name='Batch source analysis')
        expect(batch).to_contain_text('Analyse 3 links · this course');expect(batch).to_contain_text('API quota')
        assert not calls and not hub.requests.list(aid)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.screenshot(path=str(artifact/f'batch-consent-{width}.png'),full_page=True)
        batch.get_by_role('button',name='Approve and analyse 3 links',exact=True).click()
        expect(batch).to_contain_text('3 / 3 completed · 0 failed · done',timeout=15000)
        assert len(calls)==3 and not svc.get(aid,unrelated)['analysis_request_id']
        page.reload();expect(nudge).to_have_count(0)
        expect(listing.get_by_role('button',name='Analyse all (0)',exact=True)).to_be_disabled()
        expect(batch).to_contain_text('3 / 3 completed')
        page.screenshot(path=str(artifact/f'batch-done-{width}.png'),full_page=True)
        assert not errors,errors
        browser.close()


def test_batch_cancel_ui_and_item_scope(hub,account,source,monkeypatch):
    import threading
    from playwright.sync_api import expect,sync_playwright
    from canvas_hub.assistant.providers import Turn
    from .test_external_sources import URL
    svc,sid,calls=source;aid=account['id'];origin=svc.get(aid,sid)['origins'][0]['id']
    second=svc.register(aid,URL+'&page=2',course_id='4242')
    entered,resume=threading.Event(),threading.Event()
    class Provider:
        def complete(self,*args):
            entered.set();resume.wait(15);return Turn(json.dumps(RESULT))
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',lambda cfg:Provider())
    with serve(hub,[]) as (base,token,api),sync_playwright() as p:
        browser=p.chromium.launch();page=browser.new_page(viewport={'width':380,'height':900})
        page.goto(f'{base}/?token={token}');page.goto(f'{base}/#/item/{origin}')
        listing=page.locator('.external-source-list')
        expect(listing.get_by_role('button',name='Analyse all (1)',exact=True)).to_be_visible()
        listing.get_by_role('button',name='Analyse all (1)',exact=True).click()
        batch=page.get_by_role('region',name='Batch source analysis')
        expect(batch).to_contain_text('Analyse 1 links · this item')
        page.goto(f'{base}/#/course/4242')
        listing.get_by_role('button',name='Analyse all (2)',exact=True).click()
        batch.get_by_role('button',name='Approve and analyse 2 links',exact=True).click()
        assert entered.wait(5)
        try:
            batch.get_by_role('button',name='Cancel batch',exact=True).click()
            expect(batch).to_contain_text('cancelled')
            expect(listing.get_by_role('button',name='Analyse all (1)',exact=True)).to_be_visible()
            page.goto(f'{base}/#/item/{origin}')
            expect(page.get_by_role('heading',name='Analysis · this course',exact=True)).to_have_count(0)
        finally:resume.set()
        import time
        for _ in range(50):
            row=hub.db.one("SELECT COUNT(*) n FROM source_analysis_attempts WHERE state='running'")
            if not row['n']:break
            time.sleep(.05)
        assert not svc.get(aid,sid)['classification'] and not svc.get(aid,second)['analysis_request_id']
        browser.close()


def test_site_connection_requires_review_and_consent_in_browser(hub, account, source, monkeypatch):
    import threading
    from playwright.sync_api import expect, sync_playwright
    from .test_site_connection import FakeBrowser
    created=[]; redirect_state={}; closing=threading.Event(); release=threading.Event()
    class Browser(FakeBrowser):
        def snapshot(self): return {**super().snapshot(), **(redirect_state if self.state not in ('closing','closed') else {})}
        def call(self, action, arg=None, **kw):
            if action=='cancel':
                self.state='closing'; closing.set(); release.wait(5)
            return super().call(action,arg,**kw)
    def make_browser(profile,target,domains):
        created.append((profile,target,domains))
        return Browser(profile,target,domains)
    monkeypatch.setattr('canvas_hub.services.site_connection.BrowserConnection',make_browser)
    with serve(hub,[]) as (base,token,_),sync_playwright() as p:
        browser=p.chromium.launch(); page=browser.new_page(viewport={'width':380,'height':900})
        errors=[]; page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(f'{base}/?token={token}'); page.goto(f'{base}/#/course/4242')
        listing=page.locator('.external-source-list')
        listing.get_by_role('button',name='Workspace actions',exact=True).click()
        listing.locator('.workspace-action-icon[aria-label="Analyse link"]').click()
        panel=page.get_by_role('complementary',name='Source Agent',exact=True)
        panel.get_by_text('More options and diagnostics',exact=True).click()
        panel.get_by_role('button',name='Connect manually',exact=True).click()
        box=panel.get_by_role('region',name='Site connection')
        expect(box.get_by_role('button',name='Agree and open sign-in window')).to_be_visible()
        box.get_by_text('Additional sign-in domains',exact=True).click()
        assert not created
        box.get_by_label('Additional identity-provider origins').fill('https://identity.example.edu')
        box.get_by_role('button',name='Review sign-in domains',exact=True).click()
        expect(box).to_contain_text('https://identity.example.edu')
        expect(box).to_contain_text('isolated local profile')
        assert not created
        box.get_by_role('button',name='Agree and open sign-in window').click()
        expect(box).to_contain_text('Connection: opening')
        assert len(created)==1 and 'https://identity.example.edu' in created[0][2]
        redirect_state.update(state='login_open',redirect_blocked=True,redirect_reason='post_replay')
        box.get_by_text('Connection details',exact=True).click()
        box.get_by_role('button',name='Refresh connection status').click()
        box.get_by_text('Connection details',exact=True).click()
        expect(box).to_contain_text('The sign-in flow would repeat a submission')
        expect(box).to_contain_text('saved source data is unchanged')
        box.get_by_role('button',name='Close connection').click()
        assert closing.wait(2)
        expect(box).to_contain_text('Closing site connection')
        expect(box.get_by_role('button',name='Agree and open sign-in window')).to_have_count(0)
        expect(box.get_by_role('button',name='Reopen sign-in page')).to_have_count(0)
        release.set()
        expect(box.get_by_role('button',name='Agree and open sign-in window')).to_be_visible()
        assert not errors
        browser.close()


def test_connected_page_review_analysis_and_saved_source(hub, account, source, monkeypatch):
    from playwright.sync_api import expect, sync_playwright
    from canvas_hub.assistant.providers import Turn
    from .test_site_connection import FakeBrowser
    from .test_connected_analysis import COURSE
    calls=[]
    class Browser(FakeBrowser):
        def __init__(self,*args): super().__init__(*args); self.state='login_open'
        def call(self,action,arg=None,**kwargs):
            if action=='verify': self.state='content_visible'; return {'state':self.state,'content':COURSE,'review_token':'fixture-review'}
            if action=='confirm_content': self.state='connected'; return {'state':self.state}
            if action=='analysis_snapshot': return {'content':COURSE}
            return super().call(action,arg,**kwargs)
    class Provider:
        def complete(self,system,transcript,tools):
            calls.append(transcript)
            return Turn(json.dumps({'roles':['material'],'confidence':0.8,'summary':'fixture'}))
    monkeypatch.setattr('canvas_hub.services.site_connection.BrowserConnection',Browser)
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',lambda cfg:Provider())
    svc,sid,_=source; aid=account['id']
    with serve(hub,[]) as (base,token,_),sync_playwright() as p:
        browser=p.chromium.launch();page=browser.new_page();errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(f'{base}/?token={token}');page.goto(f'{base}/#/course/4242')
        listing=page.locator('.external-source-list')
        listing.get_by_role('button',name='Workspace actions',exact=True).click()
        listing.locator('.workspace-action-icon[aria-label="Analyse link"]').click()
        panel=page.get_by_role('complementary',name='Source Agent',exact=True)
        panel.get_by_text('More options and diagnostics',exact=True).click()
        panel.get_by_role('button',name='Connect manually',exact=True).click()
        box=panel.get_by_role('region',name='Site connection')
        box.get_by_role('button',name='Agree and open sign-in window').click()
        expect(box).to_contain_text('Connection: login open')
        box.get_by_role('button',name='I’ve signed in — check page').click()
        page.wait_for_timeout(1600)  # Status polling must not discard this review.
        box.get_by_role('button',name='Confirm this is the requested course page').click()
        expect(box).to_contain_text('Course page confirmed')
        box.get_by_role('button',name='Review connected source analysis').click()
        expect(box).to_contain_text('Exact text to send and save')
        page.wait_for_timeout(1600)
        assert 'NeverSendThis' not in box.locator('pre').inner_text() and not calls
        box.get_by_role('button',name='Approve, analyse and save').click()
        expect(panel).to_contain_text('Stored observations',timeout=15000)
        assert len(calls)==1 and 'NeverSendThis' not in json.dumps(calls)
        assert svc.get(aid,sid)['classification']['access_mode']=='connected_local_reviewed'
        assert svc.get(aid,sid)['records'] and not errors
        browser.close()
