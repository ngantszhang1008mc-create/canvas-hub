"""First disclosure, direct repeated action and persistent contextual output."""
import pytest
from .test_components_ui import serve
from .test_study_companion import study

pytestmark=pytest.mark.browser

@pytest.mark.parametrize('width',[1280,380])
def test_explicit_action_discloses_then_refreshes_checklist(study,width):
    from playwright.sync_api import sync_playwright,expect
    api,aid,iid,course=study
    plan=[('show_checklist',{'title':'Breakdown','sections':[{'id':'s','rows':[{'id':'r','label':'Read the rubric'}]}]})]
    with serve(api.hub,plan) as (base,token,_),sync_playwright() as p:
        browser=p.chromium.launch()
        page=browser.new_page(viewport={'width':width,'height':900})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(f'{base}/?token={token}#/item/{iid}')
        page.get_by_role('button',name='Workspace actions',exact=True).click()
        page.get_by_role('button',name='Break down assignment',exact=True).click()
        if page.get_by_role('button',name='Prepare assignment breakdown',exact=True).is_visible():
            page.get_by_role('button',name='Prepare assignment breakdown',exact=True).click()
        expect(page.get_by_role('button',name='Allow and start',exact=True)).to_be_visible()
        assert api.hub.db.one('SELECT COUNT(*) n FROM assistant_messages')['n']==0
        page.get_by_role('button',name='Allow and start',exact=True).click()
        expect(page.locator('.workspace-linked').get_by_text('Breakdown',exact=True)).to_be_visible(timeout=15000)
        assert len(api.hub.components.list(aid,item_id=iid))==1
        assert page.locator('.workspace-context').evaluate('el => el.classList.contains("agent-open")')
        page.get_by_role('button',name='Close Agent',exact=True).click()
        expect(page.get_by_role('complementary',name='Agent',exact=True)).to_be_hidden()
        page.reload()
        expect(page.locator('.workspace-linked').get_by_text('Breakdown',exact=True)).to_be_visible()
        assert not errors
        browser.close()
