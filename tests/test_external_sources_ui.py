"""Real browser workflow; the connected Agent and network transport are local fakes."""
import os
import json
from pathlib import Path

import pytest

from .test_components_ui import serve
from .test_external_sources import source, classify, SPEC
from .test_source_analysis import RESULT

pytestmark=pytest.mark.browser


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
