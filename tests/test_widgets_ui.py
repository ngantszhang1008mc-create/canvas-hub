"""Live home controls, shared state, placement review and responsive visual evidence."""
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright
from canvas_hub.testing.widget_demo import seed_widget_examples
from .test_components_ui import serve

pytestmark=pytest.mark.browser

@pytest.fixture
def widget_examples(hub,fixture_pkg):
    return seed_widget_examples(hub,fixture_pkg)


def open_manager(page, touch=False):
    toolbar=page.locator('.overall-widget-toolbar')
    toolbar.get_by_role('button',name='Workspace actions',exact=True).click()
    toolbar.get_by_role('button',name='Manage widgets',exact=True).click()
    if touch:toolbar.locator('.workspace-action-confirm').click()
    return page.get_by_role('dialog',name='Manage widgets',exact=True)


def card(page,cid):
    return page.locator(f'.overall-widget[data-widget-key="component:{cid}"]')


@pytest.mark.parametrize('theme',['light','dark'])
@pytest.mark.parametrize('width',[1280,380],ids=['desktop','phone'])
def test_direct_controls_shared_state_focus_and_layout(hub,widget_examples,theme,width,tmp_path):
    e=widget_examples
    with serve(hub,[]) as (base,token,_),sync_playwright() as p:
        browser=p.chromium.launch();page=browser.new_page(viewport={'width':width,'height':1000},has_touch=width==380,is_mobile=width==380)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(f'{base}/?token={token}#/overall')
        page.get_by_role('button',name=theme.title(),exact=True).click()
        daily=card(page,e['daily']);score=card(page,e['score']);seminar=card(page,e['seminar'])
        expect(daily.get_by_role('checkbox',name='Read course updates')).to_be_visible()
        expect(score.get_by_label('Points for Reflection (slider)',exact=True)).to_be_visible()
        expect(seminar).to_contain_text('Data seminar · synthetic example')
        expect(seminar).to_contain_text('Source · read')
        expect(score).to_contain_text('Your chosen HMAW score planning week')
        # Pointer actions and keyboard repeat survive component redraw and save acknowledgment.
        checkbox=daily.get_by_role('checkbox',name='Read course updates')
        checkbox.check();expect(checkbox).to_be_focused()
        expect(daily.locator('.save-state')).to_contain_text('Saved')
        expect(checkbox).to_be_focused()
        page.keyboard.press('Space');expect(checkbox).not_to_be_checked()
        page.keyboard.press('Space');expect(checkbox).to_be_checked()
        slider=score.get_by_label('Points for Reflection (slider)',exact=True)
        slider.focus();page.keyboard.press('ArrowLeft');page.keyboard.press('ArrowLeft')
        expect(slider).to_have_value('28');expect(slider).to_be_focused()
        expect(score.locator('.save-state')).to_contain_text('Saved')
        expect(slider).to_be_focused()
        score.get_by_label('Include Seminars',exact=True).uncheck()
        expect(score.locator('.sl-big .num')).to_have_text('73')
        seminar.get_by_role('checkbox',name='Bring your prepared question').check()
        expect(seminar.locator('.save-state')).to_contain_text('Saved')
        expect(daily.locator('.save-state')).to_contain_text('Saved')
        expect(score.locator('.save-state')).to_contain_text('Saved')
        # Every slider fits its own card on a 380px screen; no horizontal inner clipping.
        for control in score.locator('input').all():
            if control.is_visible():
                box=control.bounding_box();container=score.bounding_box()
                assert box['x']>=container['x'] and box['x']+box['width']<=container['x']+container['width']+1
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        if width==1280:
            assert abs(daily.bounding_box()['y']-score.bounding_box()['y'])<2
        else:assert score.bounding_box()['y']>daily.bounding_box()['y']
        out=Path(os.environ.get('CANVAS_HUB_SCREENSHOTS',str(tmp_path)));out.mkdir(parents=True,exist_ok=True)
        page.evaluate("document.querySelectorAll('.saved-cmp').forEach(el=>el.scrollTop=0); window.scrollTo(0,0)")
        page.screenshot(path=str(out/f'overall-widgets-{theme}-{width}.png'),full_page=True)
        score.locator('.widget-origin').click()
        planning=page.locator('details.workspace-evidence').filter(has=page.locator('.cmp-score'))
        planning.locator(':scope > summary').click()
        expect(planning.get_by_label('Points for Reflection',exact=True)).to_have_value('28')
        expect(planning.get_by_label('Include Seminars',exact=True)).not_to_be_checked()
        page.go_back();expect(card(page,e['daily']).get_by_role('checkbox',name='Read course updates')).to_be_checked()
        page.go_forward();expect(page.locator('.workspace-context')).to_be_visible()
        page.goto(f'{base}/#/course/4242')
        expect(page.get_by_role('checkbox',name='Bring your prepared question')).to_be_checked()
        page.goto(f'{base}/#/overall');page.reload()
        expect(card(page,e['score']).get_by_label('Points for Reflection',exact=True)).to_have_value('28')
        assert hub.components.get(e['aid'],e['score'])['state']['values']['reflection']==28
        assert hub.db.one('SELECT COUNT(*) n FROM assistant_messages')['n']==0
        assert not errors
        browser.close()


def test_pin_hide_reorder_condition_editor_and_agent_review(hub,widget_examples):
    e=widget_examples
    with serve(hub,[]) as (base,token,_),sync_playwright() as p:
        browser=p.chromium.launch();page=browser.new_page(viewport={'width':380,'height':900},has_touch=True,is_mobile=True)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(f'{base}/?token={token}#/overall')
        dlg=open_manager(page,True)
        score=dlg.locator(f'[data-widget-key="component:{e["score"]}"]')
        score.get_by_label('Placement for HMAW score calculator',exact=True).select_option('library')
        expect(dlg).to_contain_text('Widget settings saved')
        dlg.get_by_role('button',name='Close widget library',exact=True).click()
        expect(page.locator('.overall-widget-toolbar .workspace-launcher-toggle')).to_be_focused()
        expect(card(page,e['score'])).to_be_hidden();page.reload()
        expect(card(page,e['score'])).to_have_count(0)
        dlg=open_manager(page,True)
        score=dlg.locator(f'[data-widget-key="component:{e["score"]}"]')
        score.get_by_label('Placement for HMAW score calculator',exact=True).select_option('pinned')
        expect(dlg).to_contain_text('Widget settings saved')
        score.get_by_role('button',name='Move HMAW score calculator earlier',exact=True).click()
        expect(dlg.locator('.widget-library-row').first).to_contain_text('HMAW score calculator')
        score.get_by_role('button',name='Set condition',exact=True).click()
        score.get_by_label('Condition for HMAW score calculator',exact=True).select_option('date_window')
        score.get_by_label('From (inclusive)',exact=True).fill('2000-01-01')
        score.get_by_label('Through (inclusive)',exact=True).fill('2000-01-02')
        score.get_by_label('Why show this now?',exact=True).fill('A past user-defined window')
        score.get_by_role('button',name='Save condition',exact=True).click()
        expect(score).to_contain_text('Waiting')
        dlg.get_by_role('button',name='Close widget library',exact=True).click()
        expect(card(page,e['score'])).to_be_hidden()
        hub.widgets.propose(e['aid'],e['score'],{'mode':'pinned'})
        page.reload();expect(card(page,e['score'])).to_have_count(0)
        expect(page.locator('.overall-widget-toolbar')).to_contain_text('1 widget suggestion')
        dlg=open_manager(page,True)
        score=dlg.locator(f'[data-widget-key="component:{e["score"]}"]')
        expect(score).to_contain_text('conditional → pinned')
        score.get_by_role('button',name='Approve placement',exact=True).click()
        expect(score.get_by_label('Placement for HMAW score calculator',exact=True)).to_have_value('pinned')
        dlg.get_by_role('button',name='Close widget library',exact=True).click()
        expect(card(page,e['score'])).to_be_visible()
        page.reload()
        expect(page.locator('.overall-widget:visible').first).to_contain_text('HMAW score calculator')
        assert not errors
        browser.close()


def test_projector_and_hours_controls_are_operable_on_narrow_home(hub,widget_examples):
    from .test_phase3 import COURSE
    e=widget_examples
    projector=hub.components.create(e['aid'],'grade_projector',COURSE,course_id='4242')
    hours=hub.components.create(e['aid'],'hours_tracker',{'title':'Service hours','target':20,'minimum':10,'points':20},course_id='4242')
    for c in (projector,hours):hub.widgets.save(e['aid'],f"component:{c['id']}",{'mode':'pinned'},expected_version=0)
    with serve(hub,[]) as (base,token,_),sync_playwright() as p:
        browser=p.chromium.launch();page=browser.new_page(viewport={'width':380,'height':1000})
        page.goto(f'{base}/?token={token}#/overall')
        projection=card(page,projector['id']);tracker=card(page,hours['id'])
        slider=projection.get_by_label('What-if score for Final (slider)',exact=True)
        expect(slider).to_be_visible()
        slider.focus();page.keyboard.press('End')
        expect(projection.get_by_label('What-if score for Final',exact=True)).to_have_value('100')
        expect(projection.locator('.save-state')).to_contain_text('Saved')
        tracker.get_by_label('Hours',exact=True).fill('12')
        tracker.get_by_label('Note',exact=True).fill('Synthetic service session')
        tracker.get_by_role('button',name='Log hours',exact=True).click()
        expect(tracker.locator('.save-state')).to_contain_text('Saved')
        expect(tracker.locator('.sl-big .num')).to_have_text('12')
        for component in (projection,tracker):
            for control in component.locator('input').all():
                if control.is_visible():
                    control.scroll_into_view_if_needed()
                    box=control.bounding_box();container=component.bounding_box()
                    assert box['x']>=container['x'] and box['x']+box['width']<=container['x']+container['width']+1
        tracker.get_by_role('button',name='↶ Undo',exact=True).click()
        expect(tracker.locator('.sl-big .num')).to_have_text('0')
        page.reload()
        expect(card(page,projector['id']).get_by_label('What-if score for Final',exact=True)).to_have_value('100')
        assert hub.components.get(e['aid'],hours['id'])['state']['entries']==[]
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        browser.close()


def test_event_picker_and_uncertain_source_review(hub,widget_examples):
    e=widget_examples
    with serve(hub,[]) as (base,token,_),sync_playwright() as p:
        browser=p.chromium.launch();page=browser.new_page(viewport={'width':1280,'height':900})
        page.goto(f'{base}/?token={token}#/overall')
        dlg=open_manager(page)
        row=dlg.locator(f'[data-widget-key="component:{e["seminar"]}"]')
        row.get_by_role('button',name='Set condition',exact=True).click()
        row.get_by_label('Stored calendar event',exact=True).select_option(str(e['event']))
        row.get_by_role('button',name='Save condition',exact=True).click()
        expect(row).to_contain_text('Showing now')
        dlg.get_by_role('button',name='Close widget library',exact=True).click()
        hub.db.conn.execute("UPDATE item_fields SET last_observed_at='2000-01-01T00:00:00Z' WHERE item_id=? AND field='start_at'",(e['event'],))
        page.reload()
        expect(card(page,e['seminar'])).to_have_count(0)
        expect(page.locator('.overall-widget-toolbar')).to_contain_text('1 condition needs review')
        dlg=open_manager(page)
        expect(dlg.locator(f'[data-widget-key="component:{e["seminar"]}"]')).to_contain_text('Source is stale')
        browser.close()
