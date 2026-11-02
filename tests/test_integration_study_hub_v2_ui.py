"""Actual browser + actual stage queue + local fake model/page fixtures."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from canvas_hub.services.external_reader import PublicReader, Read
from .test_collection_pipeline import Control, build, wait_for, fast_extraction
from .test_components_ui import serve
from .test_integration_study_hub import integrated

pytestmark=pytest.mark.browser


@pytest.mark.parametrize('width',[1280,380])
def test_analysis_remains_usable_during_slow_collection_and_widgets_recover(hub,integrated,fast_extraction,width,tmp_path):
    seed,transport=integrated;aid=seed['aid'];account=hub.identity.get_account(aid)
    url='https://course.example.edu/synthetic/v2-notes'
    sid=hub.external_sources.register(aid,url,course_id='5555',title='New notes · synthetic')
    reads=[]
    def public_read(target):
        reads.append(target)
        return Read(target,200,'text/html',b'<p>Synthetic course notes.</p>') if target==url else transport(target)
    hub.external_sources.reader=PublicReader(public_read)
    hub.components.set_state(aid,seed['external_checklist'],{'done':{'reflection':True}})
    downloads=Control();run=build(hub,account,downloads,count=3);run.scoped_courses=['5555']
    answer=json.dumps({'roles':['material'],'confidence':.9,'summary':'Observed fixture notes'})
    with serve(hub,[],answer=answer) as (base,token,_),sync_playwright() as p,ThreadPoolExecutor(max_workers=1) as executor:
        browser=p.chromium.launch();page=browser.new_page(viewport={'width':width,'height':1000},has_touch=width==380,is_mobile=width==380)
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        transport.select('updated');future=executor.submit(run.run)
        try:
            wait_for(lambda:hub.jobs.get(run.job_id)['stats'].get('core_status')=='updated')
            wait_for(lambda:len(downloads.calls)==2)
            page.goto(f'{base}/?token={token}#/collect')
            card=page.locator(f'.collection-job[data-job-id="{run.job_id}"]')
            expect(card).to_contain_text('Core data updated')
            expect(card.locator('[data-phase="downloads"]')).to_contain_text('1 pending')
            expect(page.get_by_role('checkbox',name='Concurrent Canvas requests (Experimental)',exact=True)).not_to_be_checked()
            artifacts=Path(os.environ.get('CANVAS_HUB_INTEGRATION_SCREENSHOTS',str(tmp_path)));artifacts.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(artifacts/f'v2-core-ready-{width}.png'),full_page=True)
            page.goto(f'{base}/#/course/5555')
            expect(page.get_by_role('complementary',name='Uninspected external links')).to_be_visible()
            listing=page.locator('.external-source-list')
            listing.get_by_role('button',name='Analyse all (1)',exact=True).click()
            batch=page.get_by_role('region',name='Batch source analysis')
            expect(batch).to_contain_text('API quota')
            assert url not in reads
            batch.get_by_role('button',name='Approve and analyse 1 links',exact=True).click()
            expect(batch).to_contain_text('1 / 1 completed · 0 failed · done',timeout=10000)
            assert not future.done() and not downloads.release.is_set()
            assert reads.count(url)==1 and hub.external_sources.get(aid,sid)['classification']
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.screenshot(path=str(artifacts/f'v2-analysis-during-download-{width}.png'),full_page=True)
            downloads.release.set();assert future.result(timeout=10)=='completed'
            page.goto(f'{base}/#/collect')
            expect(card.locator('[data-phase="downloads"]')).to_contain_text('3 done')
            page.goto(f'{base}/#/overall')
            checklist=page.locator(f'.overall-widget[data-widget-key="component:{seed["external_checklist"]}"]')
            expect(checklist.get_by_role('checkbox',name='Reflection revised · synthetic',exact=True)).to_be_checked()
            assert hub.search.search(aid,'synthetic extracted text')['results']
            page.screenshot(path=str(artifacts/f'v2-background-complete-{width}.png'),full_page=True)
            assert not errors,errors
        finally:
            downloads.release.set();browser.close()
