"""Overall's optional agenda uses Planner dates without inventing planned starts."""
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import time

import pytest
from playwright.sync_api import expect,sync_playwright
from canvas_hub.boundary import HubAPI
from .conftest import variant
from .test_components_ui import serve
from .test_workspace_ui import daily_work

pytestmark=pytest.mark.browser

@pytest.fixture
def scheduled(hub,account,daily_work,fixture_pkg):
    aid,ids,checklist,task=daily_work
    local=datetime.now(ZoneInfo('Asia/Hong_Kong'))
    due=local.replace(hour=20,minute=0,second=0,microsecond=0)
    pkg=variant(fixture_pkg,'schedule-today',(local+timedelta(seconds=1)).isoformat())
    item=next(i for i in pkg['items'] if i['type']=='assignment' and i['canvas_id']=='7001')
    item['fields']['due_at'].update(iso=due.isoformat(),raw='Tonight')
    hub.importer.import_package(pkg)
    hub.planner.add_class(aid,title='Morning lecture',weekday=local.weekday(),start='10:00',end='11:00',course_id='4242')
    hub.annotations.create(aid,kind='task',title='Read lecture notes',item_id=ids['7001'],local_due=due.replace(hour=9).isoformat())
    return aid,ids,local

@pytest.mark.parametrize('theme',['light','dark'])
@pytest.mark.parametrize('width',[1280,380])
def test_schedule_is_optional_and_due_is_separate_from_start(hub,account,scheduled,width,theme,tmp_path):
    aid,ids,local=scheduled
    with serve(hub,[]) as (base,token,api),sync_playwright() as p:
        real=api.planner_week
        def planner(*args,**kwargs):
            week=real(*args,**kwargs)
            if kwargs.get('suggest_study'):
                start=week['week_start']+'T15:00:00+08:00'
                week['days'][0]['events'].append({'kind':'study','title':'Suggested proposal work','start':start,'end':week['week_start']+'T16:00:00+08:00','suggested':True,'for_item_id':ids['7001']})
            return week
        api.planner_week=planner
        browser=p.chromium.launch()
        page=browser.new_page(viewport={'width':width,'height':900})
        requests=[];errors=[]
        page.on('request',lambda r:requests.append(r.url) if '/planner/week' in r.url else None)
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(f'{base}/?token={token}#/overview')
        expect(page.locator('.workspace-work-row').first).to_be_visible()
        assert not requests
        expect(page.get_by_role('button',name='Tasks',exact=True)).to_have_attribute('aria-pressed','true')
        page.get_by_role('button',name=theme.title(),exact=True).click()
        page.get_by_label('Filter by course').select_option('4242')
        page.get_by_role('button',name='Schedule',exact=True).click()
        expect(page.get_by_role('heading',name="This week's schedule",exact=True)).to_be_visible()
        expect(page.locator('.workspace-schedule-event.class')).to_contain_text('Starts 10:00–11:00')
        deadline=page.locator('.workspace-schedule-event.deadline').filter(has_text='Project proposal')
        expect(deadline).to_contain_text('Deadline due 20:00')
        expect(deadline).not_to_contain_text('Starts')
        task=page.locator('.workspace-schedule-event.task').filter(has_text='Read lecture notes')
        expect(task).to_contain_text('Task due 09:00')
        expect(task).not_to_contain_text('09:30')
        expect(page.locator('.workspace-schedule-note')).to_contain_text('not planned starts')
        expect(page.locator('.workspace-schedule-note')).to_contain_text('may be stale')
        expect(page.get_by_label('Filter by course')).to_be_hidden()
        before=hub.db.one("SELECT COUNT(*) n FROM annotations WHERE account_id=?",(aid,))['n']
        page.get_by_label('Show suggested study blocks').check()
        suggested=page.locator('.workspace-schedule-event.study').filter(has_text='Suggested proposal work')
        expect(suggested).to_contain_text('Suggested start 15:00–16:00')
        expect(suggested).to_contain_text('not saved')
        assert hub.db.one("SELECT COUNT(*) n FROM annotations WHERE account_id=?",(aid,))['n']==before
        week=page.locator('.workspace-week-label').inner_text()
        page.get_by_role('button',name='Next schedule week',exact=True).click()
        expect(page.locator('.workspace-week-label')).not_to_have_text(week)
        page.get_by_role('button',name='Previous schedule week',exact=True).click()
        expect(page.locator('.workspace-week-label')).to_have_text(week)
        page.screenshot(path=str(tmp_path/f'schedule-{theme}-{width}.png'),full_page=True)
        page.get_by_role('button',name='Tasks',exact=True).click()
        expect(page.get_by_label('Filter by course')).to_have_value('4242')
        expect(page.locator('.workspace-work-row').first).to_be_visible()
        expect(page.locator('.workspace-schedule-day')).to_have_count(0)
        page.reload()
        expect(page.get_by_role('button',name='Tasks',exact=True)).to_have_attribute('aria-pressed','true')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
        browser.close()


def test_failed_or_late_schedule_does_not_replace_tasks(hub,account,scheduled):
    with serve(hub,[]) as (base,token,api),sync_playwright() as p:
        real=api.planner_week
        def slow(*a,**k):
            time.sleep(.5)
            return real(*a,**k)
        api.planner_week=slow
        browser=p.chromium.launch();page=browser.new_page(viewport={'width':380,'height':900})
        page.goto(f'{base}/?token={token}#/overview')
        expect(page.locator('.workspace-work-row').first).to_be_visible()
        with page.expect_request(lambda r:'/planner/week' in r.url):
            page.get_by_role('button',name='Schedule',exact=True).click()
        page.get_by_role('button',name='Tasks',exact=True).click()
        page.wait_for_timeout(700)
        expect(page.locator('.workspace-work-row').first).to_be_visible()
        expect(page.locator('.workspace-schedule-day')).to_have_count(0)
        page.route('**/planner/week?*',lambda route:route.fulfill(status=503,json={'message':'Planner temporarily unavailable'}))
        page.get_by_role('button',name='Schedule',exact=True).click()
        expect(page.locator('.workspace-notice')).to_contain_text('Schedule unavailable: Planner temporarily unavailable')
        page.get_by_role('button',name='Tasks',exact=True).click()
        expect(page.locator('.workspace-work-row').first).to_be_visible()
        browser.close()
