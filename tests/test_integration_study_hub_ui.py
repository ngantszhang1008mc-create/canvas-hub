"""Real-browser cross-feature paths, with only in-process source/model fixtures."""
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from canvas_hub.testing.integration_demo import propose_fixture_adapter
from .test_components_ui import serve
from .test_integration_study_hub import home_widget, integrated

pytestmark = pytest.mark.browser


def launch(host, name, touch):
    activate = lambda control: control.tap() if touch else control.click()
    activate(host.get_by_role('button', name='Workspace actions', exact=True))
    activate(host.locator(f'.workspace-action-icon[aria-label="{name}"]'))
    if touch: host.locator('.workspace-action-confirm').tap()


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('width', [1280, 380])
def test_source_refresh_home_controls_panels_and_stale_recovery(hub, integrated, theme, width, tmp_path):
    seed, transport = integrated
    aid, sid = seed['aid'], seed['source']; touch = width == 380
    with serve(hub, []) as (base, token, _), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width':width, 'height':1000}, has_touch=touch, is_mobile=touch)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'{base}/?token={token}#/overall')
        page.get_by_role('button', name=theme.title(), exact=True).click()
        card = lambda cid: page.locator(f'.overall-widget[data-widget-key="component:{cid}"]')
        checklist, score = card(seed['external_checklist']), card(seed['score'])
        checkbox = checklist.get_by_role('checkbox', name='Reflection · synthetic', exact=True)
        checkbox.check(); expect(checklist.locator('.save-state')).to_contain_text('Saved')
        slider = score.get_by_label('Points for Reflection · synthetic (slider)', exact=True)
        slider.focus(); page.keyboard.press('ArrowRight')
        expect(slider).to_have_value('19'); expect(score.locator('.save-state')).to_contain_text('Saved')
        expect(slider).to_be_focused()
        checklist.locator('.widget-origin').click()
        expect(page.get_by_role('checkbox', name='Reflection · synthetic', exact=True)).to_be_checked()
        page.go_back(); expect(card(seed['external_checklist']).get_by_role('checkbox', name='Reflection · synthetic', exact=True)).to_be_checked()
        page.go_forward(); expect(page.locator('.workspace-context')).to_be_visible()
        page.goto(f'{base}/#/course/5555')
        launch(page.locator('.external-source-list'), 'Review source', touch)
        source_panel = page.get_by_role('complementary', name='Source Agent', exact=True)
        expect(source_panel.get_by_role('button', name='Refresh without Agent', exact=True)).to_be_visible()
        # Both modules share the context shell and > launcher, but preserve their own panels.
        launch(page.locator('.workspace-context-toolbar'), 'Ask Agent', touch)
        agent_panel = page.get_by_role('complementary', name='Agent', exact=True)
        expect(agent_panel).to_be_visible(); expect(source_panel).to_be_hidden()
        agent_panel.get_by_role('button', name='Close Agent', exact=True).click()
        launch(page.locator('.external-source-list'), 'Review source', touch)
        transport.select('updated')
        source_panel.get_by_role('button', name='Refresh without Agent', exact=True).click()
        expect(source_panel).to_contain_text('Reflection revised · synthetic')
        page.goto(f'{base}/#/overall')
        score = card(seed['score']); checklist = card(seed['external_checklist'])
        expect(score.get_by_label('Points for Reflection revised · synthetic', exact=True)).to_have_value('19')
        expect(checklist.get_by_role('checkbox', name='Reflection revised · synthetic', exact=True)).to_be_checked()
        assert score.get_by_label('Points for Reflection revised · synthetic (slider)', exact=True).get_attribute('max') == '40'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        for control in score.locator('input').all():
            if control.is_visible():
                box, outer = control.bounding_box(), score.bounding_box()
                assert box['x'] >= outer['x'] and box['x'] + box['width'] <= outer['x'] + outer['width'] + 1
        artifacts = Path(os.environ.get('CANVAS_HUB_INTEGRATION_SCREENSHOTS', str(tmp_path)))
        artifacts.mkdir(parents=True, exist_ok=True)
        page.evaluate("document.querySelectorAll('.saved-cmp').forEach(e=>e.scrollTop=0);scrollTo(0,0)")
        page.screenshot(path=str(artifacts/f'integration-home-{theme}-{width}.png'), full_page=True)
        # A new source approval cannot consume the independent widget proposal.
        placement = hub.widgets.propose(aid, seed['score'], {'mode':'pinned'})['proposal']
        propose_fixture_adapter(hub, aid, sid, transport, reanalyze=True)
        page.goto(f'{base}/#/course/5555')
        launch(page.locator('.external-source-list'), 'Review source', touch)
        expect(source_panel.get_by_role('button', name='Approve, test and enable', exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.screenshot(path=str(artifacts/f'integration-source-{theme}-{width}.png'), full_page=True)
        source_panel.get_by_role('button', name='Approve, test and enable', exact=True).click()
        expect(source_panel.get_by_role('button', name='Refresh without Agent', exact=True)).to_be_visible()
        assert home_widget(hub, seed, seed['score'])['mode'] == 'conditional'
        assert home_widget(hub, seed, seed['score'])['pending']['id'] == placement['id']
        transport.select('unavailable')
        source_panel.get_by_role('button', name='Refresh without Agent', exact=True).click()
        expect(source_panel).to_contain_text('previous data retained')
        page.goto(f'{base}/#/overall')
        expect(card(seed['score'])).to_have_count(0)
        expect(page.locator('.overall-widget-toolbar')).to_contain_text('1 condition needs review')
        expect(card(seed['external_checklist']).locator('.workspace-source')).to_contain_text('Source unavailable')
        expect(card(seed['external_checklist']).get_by_role('checkbox', name='Reflection revised · synthetic', exact=True)).to_be_checked()
        page.screenshot(path=str(artifacts/f'integration-stale-{theme}-{width}.png'), full_page=True)
        launch(page.locator('.overall-widget-toolbar'), 'Manage widgets', touch)
        dlg = page.get_by_role('dialog', name='Manage widgets', exact=True)
        row = dlg.locator(f'[data-widget-key="component:{seed["score"]}"]')
        expect(row).to_contain_text('External source is unavailable')
        row.get_by_role('button', name='Approve placement', exact=True).click()
        expect(row.get_by_label('Placement for HMAW score calculator', exact=True)).to_have_value('pinned')
        dlg.get_by_role('button', name='Close widget library', exact=True).click()
        expect(card(seed['score']).get_by_label('Points for Reflection revised · synthetic', exact=True)).to_have_value('19')
        expect(card(seed['score']).locator('.workspace-source')).to_contain_text('Source unavailable')
        transport.select('updated'); hub.external_sources.refresh(aid, sid)
        page.reload()
        expect(card(seed['score']).locator('.workspace-source')).to_contain_text('Source linked')
        assert len(hub.requests.list(aid)) == 2 and not hub.db.query('SELECT * FROM assistant_messages')
        assert not errors, errors
        browser.close()
