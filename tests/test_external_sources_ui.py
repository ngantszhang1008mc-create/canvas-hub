"""Real browser workflow; the connected Agent and network transport are local fakes."""
import os
from pathlib import Path

import pytest

from .test_components_ui import serve
from .test_external_sources import source, classify

pytestmark=pytest.mark.browser


@pytest.mark.parametrize('theme',['light','dark'])
@pytest.mark.parametrize('width',[1280,380])
def test_one_click_analysis_review_approval_and_cached_reload(hub,account,source,theme,width,tmp_path):
    from playwright.sync_api import expect,sync_playwright
    svc,sid,calls=source;aid=account['id']
    with serve(hub,[]) as (base,token,_),sync_playwright() as p:
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
        expect(panel).to_be_visible();expect(panel).to_contain_text('Request #')
        assert len(hub.requests.list(aid))==1 and not calls
        # Connected agent reads the fixture with its own access and completes the request.
        classify(svc,aid,sid)
        approve=panel.get_by_role('button',name='Approve, test and enable',exact=True)
        expect(approve).to_be_visible(timeout=10000)
        expect(panel).to_contain_text('public pages only')
        expect(panel).to_contain_text('SHA-256')
        # Long source URLs, exact spec and evidence remain inside the 380px layout.
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        artifact=Path(os.environ.get('CANVAS_HUB_SOURCE_SCREENSHOTS',str(tmp_path/'external-source-evidence')))
        artifact.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(artifact/f'proposal-{theme}-{width}.png'),full_page=True)
        approve.click()
        expect(panel.get_by_role('button',name='Refresh without Agent',exact=True)).to_be_visible(timeout=10000)
        expect(panel).to_contain_text('Deadline conflicts')
        assert len(calls)==1 and svc.get(aid,sid)['enabled_revision']==1
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
        page.screenshot(path=str(artifact/f'enabled-{theme}-{width}.png'),full_page=True)
        stored=next(r for r in svc.get(aid,sid)['records'] if r['item_type']=='external_link')
        page.goto(f"{base}/#/item/{stored['id']}")
        expect(page.get_by_role('heading',name='Content read by your local adapter',exact=True)).to_be_visible()
        assert not errors,errors
        browser.close()
